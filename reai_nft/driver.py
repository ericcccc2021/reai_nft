from pathlib import Path

import cdv.clibs as std_lib
from cdv.util.load_clvm import load_clvm
from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.coin_spend import CoinSpend
from chia.wallet.puzzles.load_clvm import load_clvm as load_chia_clvm
from clvm.SExp import SExp

clibs_path: Path = Path(std_lib.__file__).parent

SINGLETON_MOD = load_chia_clvm("singleton_top_layer.clvm")
LAUNCHER_PUZZLE = load_chia_clvm("singleton_launcher.clvm")
SINGLETON_LAUNCHER_HASH = LAUNCHER_PUZZLE.get_tree_hash()
REAI_MOD: Program = load_clvm(
    "reai_puzzle.clsp", "reai_nft.clsp", search_paths=[clibs_path]
)

SINGLETON_MOD_HASH = SINGLETON_MOD.get_tree_hash()
COIN_AMOUNT = 1


def singleton_puzzle(
    launcher_id: Program, launcher_puzzle_hash: bytes32, inner_puzzle: Program
) -> Program:
    return SINGLETON_MOD.curry(
        (SINGLETON_MOD_HASH, (launcher_id, launcher_puzzle_hash)), inner_puzzle
    )


def create_reai_puzzle(data, pub_key, version=1, mod=REAI_MOD) -> Program:
    return mod.curry(mod.get_tree_hash(), data, version, pub_key)


def get_inner_puzzle_reveal(coin_spend: CoinSpend) -> Program:

    if coin_spend.coin.puzzle_hash != SINGLETON_LAUNCHER_HASH:
        full_puzzle = Program.from_bytes(bytes(coin_spend.puzzle_reveal))
        r = full_puzzle.uncurry()
        if r is not None:
            _, args = r
            _, inner_puzzle = list(args.as_iter())
            return inner_puzzle


def solution_for_reai(version, commit=None, new_pub_key=None, adapt=False) -> SExp:
    if not commit:
        commit = []
    if not adapt:
        return Program.to([version, commit, new_pub_key or []])
    return Program.to([[], version, commit, new_pub_key or []])
