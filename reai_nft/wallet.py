from contextlib import asynccontextmanager
from enum import Enum
from pprint import pprint
from typing import List, Optional, Tuple

import aiohttp

from reai_nft import driver
from reai_nft.driver import get_inner_puzzle_reveal, solution_for_reai
from blspy import AugSchemeMPL, G2Element, PrivateKey
from chia.consensus.coinbase import create_puzzlehash_for_pk
from chia.consensus.default_constants import DEFAULT_CONSTANTS
from chia.rpc.full_node_rpc_client import FullNodeRpcClient
from chia.rpc.wallet_rpc_client import WalletRpcClient
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.blockchain_format.tree_hash import sha256_treehash
from chia.types.coin_record import CoinRecord
from chia.types.coin_spend import CoinSpend
from chia.types.spend_bundle import SpendBundle
from chia.util.bech32m import decode_puzzle_hash, encode_puzzle_hash
from chia.util.condition_tools import ConditionOpcode
from chia.util.config import load_config
from chia.util.default_root import DEFAULT_ROOT_PATH
from chia.util.ints import uint16, uint32, uint64
from chia.wallet.derive_keys import (
    master_sk_to_wallet_sk,
)
from chia.wallet.lineage_proof import LineageProof
from chia.wallet.puzzles import (
    p2_conditions,
    p2_delegated_puzzle_or_hidden_puzzle,
    singleton_top_layer,
)
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import (
    calculate_synthetic_secret_key,
)
from clvm.casts import int_from_bytes, int_to_bytes
import random

COIN_AMOUNT = 1


class Operation(Enum):
    ADD = 16
    REMOVE = 17


async def get_node_client(config_path=DEFAULT_ROOT_PATH) -> Optional[FullNodeRpcClient]:
    try:
        if not config_path:
            config_path = DEFAULT_ROOT_PATH
        config = load_config(config_path, "config.yaml")
        self_hostname = config["self_hostname"]
        full_node_rpc_port = config["full_node"]["rpc_port"]
        full_node_client: FullNodeRpcClient = await FullNodeRpcClient.create(
            self_hostname, uint16(full_node_rpc_port), DEFAULT_ROOT_PATH, config
        )
        return full_node_client
    except Exception as e:
        if isinstance(e, aiohttp.ClientConnectorError):
            pprint(
                f"Connection error. Check if full node is running at {full_node_rpc_port}"
            )
        else:
            pprint(f"Exception from 'harvester' {e}")
        return None


async def get_wallet_client(config_path=DEFAULT_ROOT_PATH) -> Optional[WalletRpcClient]:
    try:
        if not config_path:
            config_path = DEFAULT_ROOT_PATH
        config = load_config(config_path, "config.yaml")
        self_hostname = config["self_hostname"]
        wallet_rpc_port = config["wallet"]["rpc_port"]
        full_node_client: WalletRpcClient = await WalletRpcClient.create(
            self_hostname, uint16(wallet_rpc_port), DEFAULT_ROOT_PATH, config
        )
        return full_node_client
    except Exception as e:
        if isinstance(e, aiohttp.ClientConnectorError):
            pprint(
                f"Connection error. Check if full node is running at {wallet_rpc_port}"
            )
        else:
            pprint(f"Exception from 'wallet' {e}")
        return None


class ReaiWallet:
    def __init__(
            self,
            wallet_id: str,
            wallet_client: WalletRpcClient,
            node: FullNodeRpcClient,
            wallet_address,
            private_key: PrivateKey,
            verbose=False,
    ):
        self.wallet_client = wallet_client
        self.wallet_id = wallet_id
        self.node_client = node
        self.private_key = private_key
        self.wallet_address = wallet_address
        self.sk = master_sk_to_wallet_sk(self.private_key, uint32(0))
        self.pk = self.sk.get_g1()
        self.verbose = verbose

    @staticmethod
    @asynccontextmanager
    async def create(
            fingerprint: int = None, config_file_path: str = None, verbose=False
    ):
        bw = None
        try:
            wallet_client = await get_wallet_client(config_file_path)
            node_client = await get_node_client(config_file_path)
            if not fingerprint:
                fingerprints = await wallet_client.get_public_keys()
                if not fingerprints:
                    raise ValueError("You need at least one key to use this wallet")
                fingerprint = fingerprints[0]
            if verbose:
                print(f"Using key fingerprint: {fingerprint}")
            response = await wallet_client.log_in(fingerprint)
            if not response.get("success"):
                raise ValueError("Couldn't login to wallet, please check your wallet")
            private_key_resp = await wallet_client.get_private_key(fingerprint)
            private_key = PrivateKey.from_bytes(
                bytearray.fromhex(private_key_resp["sk"])
            )
            wallet_infos = await wallet_client.get_wallets()
            if not wallet_infos:
                raise ValueError("Wallet is empty")
            wallet_id = wallet_infos[0]["id"]
            wallet_address = encode_puzzle_hash(
                create_puzzlehash_for_pk(
                    master_sk_to_wallet_sk(private_key, uint32(0)).get_g1()
                ),
                "txch",
            )
            assert wallet_client and node_client
            bw = ReaiWallet(
                wallet_id,
                wallet_client,
                node_client,
                wallet_address,
                private_key,
                verbose=verbose,
            )
            if verbose:
                print(f"Connected to wallet: {wallet_address}")
            yield bw
        finally:
            if bw:
                await bw.close()

    async def close(self):
        self.wallet_client.close()
        self.node_client.close()
        await self.wallet_client.await_closed()
        await self.node_client.await_closed()

    async def _mutate_data(
            self, coin_name: bytes32, operation: Operation, value, fee=0
    ) -> bytes32:

        parent_record, singleton_record = await self._get_latest_singleton(coin_name)

        coin_spend = await self.node_client.get_puzzle_and_solution(
            parent_record.coin.name(), parent_record.spent_block_index
        )
        lineage_proof: LineageProof = singleton_top_layer.lineage_proof_for_coinsol(
            coin_spend
        )
        singleton: Coin = singleton_record.coin
        version, data = await self.get_data(coin_name)
        if self.verbose:
            print(f"Mutating {version=} and {data=}")
        puzzle = driver.create_reai_puzzle(data, self.pk, version=version)
        puzzle_reveal: Program = singleton_top_layer.puzzle_for_singleton(
            coin_name,
            puzzle,
        )
        new_version = version + 1
        if self.verbose:
            print(f"Applying {new_version=} with {operation=} {value=}")
        inner_solution = solution_for_reai(new_version, [operation.value, value])
        full_solution: Program = singleton_top_layer.solution_for_singleton(
            lineage_proof, singleton.amount, inner_solution
        )

        signature: G2Element = AugSchemeMPL.sign(
            self.sk,
            (
                    sha256_treehash(Program.to([operation.value, value]))
                    + singleton.name()
                    + DEFAULT_CONSTANTS.AGG_SIG_ME_ADDITIONAL_DATA
            ),
        )
        singleton_spend = SpendBundle(
            [
                CoinSpend(singleton, puzzle_reveal, full_solution),
            ],
            signature,
        )
        if fee > 0:
            fee_spend = await self._get_fee_spend_bundle(fee)
            singleton_spend = SpendBundle.aggregate([singleton_spend, fee_spend])
        if self.verbose:
            singleton_spend.debug(
                agg_sig_additional_data=DEFAULT_CONSTANTS.AGG_SIG_ME_ADDITIONAL_DATA
            )
        result = await self.node_client.push_tx(singleton_spend)
        if result and result.get("success"):
            return singleton_spend.name()
        singleton_spend.debug()
        raise Exception("Error pushing transaction: %s" % singleton_spend.name())

    async def add_pair(
            self, coin_name: bytes32, pair: Tuple[bytes, bytes], fee=0
    ) -> bool:
        if not isinstance(pair, (tuple, list)):
            raise ValueError("cons must be tuple or list")
        if len(pair) != 2:
            raise ValueError("Pairs must contain 2 items exactly")
        return await self._mutate_data(coin_name, Operation.ADD, pair, fee=fee)

    async def remove_pair_at(self, coin_name, index: int, fee=0) -> int:
        return await self._mutate_data(
            coin_name, Operation.REMOVE, int_to_bytes(index), fee=fee
        )

    async def freeze(self, coin_name, fee=0) -> bool:
        parent_record, singleton_record = await self._get_latest_singleton(coin_name)

        coin_spend = await self.node_client.get_puzzle_and_solution(
            parent_record.coin.name(), parent_record.spent_block_index
        )
        lineage_proof: LineageProof = singleton_top_layer.lineage_proof_for_coinsol(
            coin_spend
        )
        singleton: Coin = singleton_record.coin
        version, data = await self.get_data(coin_name)
        puzzle = driver.create_reai_puzzle(data, self.pk, version=version)
        puzzle_reveal: Program = singleton_top_layer.puzzle_for_singleton(
            coin_name,
            puzzle,
        )
        new_version = 0
        inner_solution = solution_for_reai(new_version)
        full_solution: Program = singleton_top_layer.solution_for_singleton(
            lineage_proof, singleton.amount, inner_solution
        )

        signature: G2Element = AugSchemeMPL.sign(
            self.sk,
            (
                    sha256_treehash(Program.to(new_version))
                    + singleton.name()
                    + DEFAULT_CONSTANTS.AGG_SIG_ME_ADDITIONAL_DATA
            ),
        )
        singleton_spend = SpendBundle(
            [
                CoinSpend(singleton, puzzle_reveal, full_solution),
            ],
            signature,
        )
        if fee > 0:
            fee_spend = await self._get_fee_spend_bundle(fee)
            singleton_spend = SpendBundle.aggregate([singleton_spend, fee_spend])
        if self.verbose:
            singleton_spend.debug()
        result = await self.node_client.push_tx(singleton_spend)
        if result and result.get("success"):
            return singleton_spend.name()
        raise Exception("Error pushing transaction: %s" % singleton_spend.name())

    async def get_data(self, coin_name) -> Tuple[int, list]:
        try:
            parent_record, _ = await self._get_latest_singleton(coin_name)
        except ValueError:
            return 1, []
        coin_spend = await self.node_client.get_puzzle_and_solution(
            parent_record.coin.name(), parent_record.spent_block_index
        )
        puzzle_reveal = get_inner_puzzle_reveal(coin_spend)
        if not puzzle_reveal:
            return 1, []
        solution_args = coin_spend.solution.to_program().rest().rest().first()
        commit = solution_args.rest().first().as_python()
        version = solution_args.first().as_python()
        version = int_from_bytes(version)
        r = coin_spend.puzzle_reveal.uncurry()
        _, args = r
        # extract curried data from previous version
        data = (
            args.rest()
                .first()
                .rest()
                .rest()
                .first()
                .rest()
                .rest()
                .first()
                .rest()
                .first()
        ).as_python()
        if len(data) == 1:
            data = int_from_bytes(data[0])
            data = []
        else:
            data = data[1:]
        if commit:
            op = int_from_bytes(commit[0])
            # manually apply last commit to data to
            # get latest version of data content
            if op == Operation.ADD.value:
                data.insert(0, commit[1])
            elif op == Operation.REMOVE.value:
                index = int_from_bytes(commit[1])
                del data[index]
            else:
                raise ValueError(f"Bad commit: {commit}")
        return version, data

    async def _get_fee_spend_bundle(self, fee):
        starting_coin = await self._find_usable_coin()
        starting_puzzle: Program = p2_delegated_puzzle_or_hidden_puzzle.puzzle_for_pk(
            self.pk
        )  # noqa
        conditions = [Program.to(
            [
                ConditionOpcode.CREATE_COIN,
                starting_coin.puzzle_hash,
                starting_coin.amount - fee,
            ]
        )]
        full_solution: Program = (
            p2_delegated_puzzle_or_hidden_puzzle.solution_for_conditions(conditions)
        )  # noqa

        starting_coinsol = CoinSpend(
            starting_coin,
            starting_puzzle,
            full_solution,
        )
        delegated_puzzle: Program = p2_conditions.puzzle_for_conditions(conditions)

        ssk = calculate_synthetic_secret_key(
            self.sk, p2_delegated_puzzle_or_hidden_puzzle.DEFAULT_HIDDEN_PUZZLE_HASH
        )
        signature: G2Element = AugSchemeMPL.sign(
            ssk,
            (
                    delegated_puzzle.get_tree_hash()
                    + starting_coin.name()
                    + DEFAULT_CONSTANTS.AGG_SIG_ME_ADDITIONAL_DATA
            ),
        )

        spend_bundle = SpendBundle([starting_coinsol], signature)
        return spend_bundle

    async def _find_usable_coin(self) -> Coin:
        puzzle_hash = decode_puzzle_hash(self.wallet_address)
        unspent_coin_records: List[
            CoinRecord
        ] = await self.node_client.get_coin_records_by_puzzle_hash(
            puzzle_hash, include_spent_coins=False
        )

        coin_record: CoinRecord
        for coin_record in unspent_coin_records:
            coin: Coin = coin_record.coin
            if coin.amount > 0 and not coin_record.spent:
                return coin
        raise ValueError("No usable coins found in the wallet. Pick another.")

    async def _find_usable_coins(self) -> List[Coin]:
        puzzle_hash = decode_puzzle_hash(self.wallet_address)
        unspent_coin_records: List[
            CoinRecord
        ] = await self.node_client.get_coin_records_by_puzzle_hash(
            puzzle_hash, include_spent_coins=False
        )
        coins_with_balance = []
        coin_record: CoinRecord
        for coin_record in unspent_coin_records:
            coin: Coin = coin_record.coin
            if coin.amount > 0 and not coin_record.spent:
                coins_with_balance.append(coin)
        if len(coins_with_balance) < 1:
            raise ValueError("No usable coins found in the wallet. Pick another.")
        return coins_with_balance

    async def split_largest_coin_into_k(self, k=10, fee=0) -> bool:
        puzzle = driver.create_reai_puzzle([], self.pk)
        starting_coins = await self._find_usable_coins()
        if not starting_coins or len(starting_coins) == 0:
            return False
        largest_coin = starting_coins[0]
        for c in starting_coins:
            if c.amount > largest_coin.amount:
                largest_coin = c

        if largest_coin.amount <= k + fee:
            return False

        (
            conditions,
            launcher_coinsol,
        ) = singleton_top_layer.launch_conditions_and_coinsol(  # noqa
            largest_coin, puzzle, Program.to([]), 1
        )
        total_available = largest_coin.amount - 1
        if k + fee < total_available:
            amount_per_coin = (total_available - fee) // k
            alreadyAdded = 0
            for x in range(0, k):
                amount_to_assign = amount_per_coin - k + x
                if x == k - 1:
                    amount_to_assign = total_available - fee - alreadyAdded
                conditions.append(
                    Program.to(
                        [
                            ConditionOpcode.CREATE_COIN,
                            largest_coin.puzzle_hash,
                            amount_to_assign,
                        ]
                    )
                )
                alreadyAdded = alreadyAdded + amount_to_assign

        full_solution: Program = (
            p2_delegated_puzzle_or_hidden_puzzle.solution_for_conditions(conditions)
        )  # noqa

        largest_coin_puzzle: Program = p2_delegated_puzzle_or_hidden_puzzle.puzzle_for_pk(
            self.pk
        )  # noqa

        largest_coinsol = CoinSpend(
            largest_coin,
            largest_coin_puzzle,
            full_solution,
        )
        delegated_puzzle: Program = p2_conditions.puzzle_for_conditions(conditions)

        ssk = calculate_synthetic_secret_key(
            self.sk, p2_delegated_puzzle_or_hidden_puzzle.DEFAULT_HIDDEN_PUZZLE_HASH
        )
        signature: G2Element = AugSchemeMPL.sign(
            ssk,
            (
                    delegated_puzzle.get_tree_hash()
                    + largest_coin.name()
                    + DEFAULT_CONSTANTS.AGG_SIG_ME_ADDITIONAL_DATA
            ),
        )

        spend_bundle = SpendBundle([largest_coinsol, launcher_coinsol], signature)
        resp = await self.node_client.push_tx(spend_bundle)
        if not resp["success"]:
            raise ValueError("Couldn't push the transaction: %s" % resp)

        return True

    async def get_number_of_coins_available(self) -> int:
        starting_coins = await self._find_usable_coins()
        if starting_coins:
            return len(starting_coins)
        return 0

    async def mint_k(self, fee=0, k=50) -> Tuple[bool, List[Tuple[bytes32, bytes32]]]:
        puzzle = driver.create_reai_puzzle([], self.pk)
        all_available_coins = await self._find_usable_coins()
        if len(all_available_coins) < k:
            return False, []
        else:
            starting_coins = random.sample(all_available_coins, k)

            starting_puzzle: Program = p2_delegated_puzzle_or_hidden_puzzle.puzzle_for_pk(
                self.pk
            )  # noqa
            spend_bundles = []
            listOfTuplesOfTxIdAndLauncherId = []
            hasDeductFee = False
            for starting_coin in starting_coins:
                (
                    conditions,
                    launcher_coinsol,
                ) = singleton_top_layer.launch_conditions_and_coinsol(  # noqa
                    starting_coin, puzzle, Program.to([]), COIN_AMOUNT
                )
                if COIN_AMOUNT < starting_coin.amount:
                    if not hasDeductFee and COIN_AMOUNT < starting_coin.amount - fee:
                        conditions.append(
                            Program.to(
                                [
                                    ConditionOpcode.CREATE_COIN,
                                    starting_coin.puzzle_hash,
                                    starting_coin.amount - COIN_AMOUNT - fee,
                                ]
                            )
                        )
                        hasDeductFee = True
                    else:
                        conditions.append(
                            Program.to(
                                [
                                    ConditionOpcode.CREATE_COIN,
                                    starting_coin.puzzle_hash,
                                    starting_coin.amount - COIN_AMOUNT,
                                ]
                            )
                        )
                full_solution: Program = (
                    p2_delegated_puzzle_or_hidden_puzzle.solution_for_conditions(conditions)
                )  # noqa

                starting_coinsol = CoinSpend(
                    starting_coin,
                    starting_puzzle,
                    full_solution,
                )
                delegated_puzzle: Program = p2_conditions.puzzle_for_conditions(conditions)

                ssk = calculate_synthetic_secret_key(
                    self.sk, p2_delegated_puzzle_or_hidden_puzzle.DEFAULT_HIDDEN_PUZZLE_HASH
                )
                signature: G2Element = AugSchemeMPL.sign(
                    ssk,
                    (
                            delegated_puzzle.get_tree_hash()
                            + starting_coin.name()
                            + DEFAULT_CONSTANTS.AGG_SIG_ME_ADDITIONAL_DATA
                    ),
                )

                spend_bundle = SpendBundle([starting_coinsol, launcher_coinsol], signature)
                spend_bundles.append(spend_bundle)
                launcher_coin: Coin = singleton_top_layer.generate_launcher_coin(
                    starting_coin,
                    uint64(COIN_AMOUNT),
                )
                listOfTuplesOfTxIdAndLauncherId.append((spend_bundle.name(), launcher_coin.name()))

            combined_spend = SpendBundle.aggregate(spend_bundles)
            resp = await self.node_client.push_tx(combined_spend)
            if not resp["success"]:
                raise ValueError("Couldn't push the transaction: %s" % resp)

            return True, listOfTuplesOfTxIdAndLauncherId

    async def mint(self, fee=0) -> Tuple[bytes32, bytes32]:
        puzzle = driver.create_reai_puzzle([], self.pk)
        starting_coin = await self._find_usable_coin()
        starting_puzzle: Program = p2_delegated_puzzle_or_hidden_puzzle.puzzle_for_pk(
            self.pk
        )  # noqa
        (
            conditions,
            launcher_coinsol,
        ) = singleton_top_layer.launch_conditions_and_coinsol(  # noqa
            starting_coin, puzzle, Program.to([]), COIN_AMOUNT
        )
        if COIN_AMOUNT < starting_coin.amount:
            conditions.append(
                Program.to(
                    [
                        ConditionOpcode.CREATE_COIN,
                        starting_coin.puzzle_hash,
                        starting_coin.amount - COIN_AMOUNT - fee,
                    ]
                )
            )
        full_solution: Program = (
            p2_delegated_puzzle_or_hidden_puzzle.solution_for_conditions(conditions)
        )  # noqa

        starting_coinsol = CoinSpend(
            starting_coin,
            starting_puzzle,
            full_solution,
        )
        delegated_puzzle: Program = p2_conditions.puzzle_for_conditions(conditions)

        ssk = calculate_synthetic_secret_key(
            self.sk, p2_delegated_puzzle_or_hidden_puzzle.DEFAULT_HIDDEN_PUZZLE_HASH
        )
        signature: G2Element = AugSchemeMPL.sign(
            ssk,
            (
                    delegated_puzzle.get_tree_hash()
                    + starting_coin.name()
                    + DEFAULT_CONSTANTS.AGG_SIG_ME_ADDITIONAL_DATA
            ),
        )

        spend_bundle = SpendBundle([starting_coinsol, launcher_coinsol], signature)
        if self.verbose:
            spend_bundle.debug(
                agg_sig_additional_data=DEFAULT_CONSTANTS.AGG_SIG_ME_ADDITIONAL_DATA
            )
        # assert False
        # assert False
        resp = await self.node_client.push_tx(spend_bundle)
        if not resp["success"]:
            raise ValueError("Couldn't push the transaction: %s" % resp)
        launcher_coin: Coin = singleton_top_layer.generate_launcher_coin(
            starting_coin,
            uint64(COIN_AMOUNT),
        )
        return spend_bundle.name(), launcher_coin.name()

    async def set_ownership(self, coin_name, new_pub_key: bytes32, fee=0) -> bool:
        parent_record, singleton_record = await self._get_latest_singleton(coin_name)

        coin_spend = await self.node_client.get_puzzle_and_solution(
            parent_record.coin.name(), parent_record.spent_block_index
        )
        lineage_proof: LineageProof = singleton_top_layer.lineage_proof_for_coinsol(
            coin_spend
        )
        singleton: Coin = singleton_record.coin
        version, data = await self.get_data(coin_name)
        puzzle = driver.create_reai_puzzle(data, self.pk, version=version)
        puzzle_reveal: Program = singleton_top_layer.puzzle_for_singleton(
            coin_name,
            puzzle,
        )
        inner_solution = solution_for_reai(version, new_pub_key=new_pub_key)
        full_solution: Program = singleton_top_layer.solution_for_singleton(
            lineage_proof, singleton.amount, inner_solution
        )

        signature: G2Element = AugSchemeMPL.sign(
            self.sk,
            (
                    sha256_treehash(Program.to(new_pub_key))
                    + singleton.name()
                    + DEFAULT_CONSTANTS.AGG_SIG_ME_ADDITIONAL_DATA
            ),
        )
        singleton_spend = SpendBundle(
            [
                CoinSpend(singleton, puzzle_reveal, full_solution),
            ],
            signature,
        )
        if fee > 0:
            fee_spend = await self._get_fee_spend_bundle(fee)
            singleton_spend = SpendBundle.aggregate([singleton_spend, fee_spend])
        if self.verbose:
            singleton_spend.debug()
        result = await self.node_client.push_tx(singleton_spend)
        if result and result.get("success"):
            return singleton_spend.name()
        raise Exception("Error pushing transaction: %s" % singleton_spend.name())

    async def _get_latest_singleton(
            self, coin_id: bytes32
    ) -> Tuple[CoinRecord, CoinRecord]:
        if self.verbose:
            print(f"Finding latest singleton for launcher: {coin_id.hex()}")
        coin_record: CoinRecord = await self.node_client.get_coin_record_by_name(
            coin_id
        )

        if not coin_record:
            raise Exception(f"Can't find coin: {coin_id.hex()}")
        if not coin_record.spent:
            # fresh reai nft, return now
            return (
                await self.node_client.get_coin_record_by_name(coin_record.parent_info),
                coin_record,
            )
        while True:
            descendants = await self.node_client.get_coin_records_by_parent_ids(
                [coin_id]
            )
            if len(descendants) != 1:
                raise ValueError("Not a singleton")
            descendant: CoinRecord = descendants[0]
            if descendant.spent:
                coin_record = descendant
                coin_id = descendant.coin.name()
            else:
                assert coin_record.spent
                return coin_record, descendant
