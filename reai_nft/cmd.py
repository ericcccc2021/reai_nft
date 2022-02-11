#!/usr/bin/env python3
import asyncio
from functools import wraps
from chia.util.byte_types import hexstr_to_bytes
import json
import click

from reai_nft.wallet import ReaiWallet

VERBOSE = False


def coro(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        return asyncio.run(f(*args, **kwargs))

    return wrapper


def debug(msg):
    global VERBOSE
    if VERBOSE:
        click.echo(msg)


def parse_launcher(ctx, param, value):
    try:
        if not value:
            raise ValueError
        if not isinstance(value, (str, bytes)):
            raise ValueError
        if len(value) != 66:
            raise click.BadArgumentUsage(
                "Launcher ID must start with 0x and be 66 chars long"
            )
        if value[:2] != "0x":
            raise click.BadArgumentUsage("Launcher ID must start with 0x")
        return hexstr_to_bytes(value)
    except click.BadArgumentUsage:
        raise
    except Exception as e:
        raise click.BadArgumentUsage("Not a valid launcher ID")


@click.group(name="reai-nft")
@click.option(
    "--config-path",
    help="Path to your Chia blockchain config (usually ~/.chia). Defaults to fetching it from CHIA_ROOT env var.",
    default=None,
)
@click.option(
    "--fingerprint",
    help="Key fingerprint, will default to first one it finds if not provided.",
    default=None,
)
@click.option("-v", "--verbose", help="Show more debugging info.", is_flag=True)
@click.pass_context
def cli(ctx, config_path, fingerprint, verbose):
    """Manage reai nft on Chia network.

    They can be used to store key information in a decentralized and durable way."""
    if verbose:
        global VERBOSE
        VERBOSE = True
    debug(f"Connecting to wallet...")
    wallet = ReaiWallet.create(fingerprint, config_path, verbose=verbose)
    ctx.obj = wallet


@click.command(
    name="mk",
    help="try to mint k tokens if possible. Otherwise it tries to split existing tokens so next time we can mint k "
         "tokens",
)
@click.option(
    "--fee",
    type=int,
    default=0,
    help="Transaction fee, defaults to 0",
)
@click.option(
    "-k",
    type=int,
    default=50,
    help="number of tokens to mint",
)
@coro
@click.pass_context
async def mint_k(ctx, fee, k):
    wallet: ReaiWallet
    async with ctx.obj as wallet:
        debug("Minting a new coin for wallet: %s" % wallet.wallet_address)
        res = await wallet.mint_k(fee=fee, k=k)
        if res[0]:
            if res[1] is not None and len(res[1]) > 0:
                for _, item in enumerate(res[1]):
                    tx_id = item[0]
                    launcher_id = item[1]
                    click.echo(
                        f"coin_id: 0x{launcher_id}\n"
                        f"tx: 0x{tx_id}\n"
                    )
                click.echo(
                    f"Fee: {fee} mojos"
                )
        else:
            click.echo("Number of coins < k")


@click.command(
    name="get-number-of-available-coins",
    help="get number of available coins to spend",
)
@coro
@click.pass_context
async def get_number_of_available_coins(ctx):
    wallet: ReaiWallet
    async with ctx.obj as wallet:
        n = await wallet.get_number_of_coins_available()
        click.echo(
            f"# of coins available: {n}\n"
        )


@click.command(
    name="split-largest-coin-into-k",
    help="split the largest coin into k coins",
)
@click.option(
    "-k",
    type=int,
    default=10,
    help="number of coins to be split into",
)
@click.option(
    "--fee",
    type=int,
    default=0,
    help="Transaction fee, defaults to 0",
)
@coro
@click.pass_context
async def split_largest_coin_into_k(ctx, k, fee):
    wallet: ReaiWallet
    async with ctx.obj as wallet:
        n = await wallet.split_largest_coin_into_k(k=k, fee=fee)
        click.echo(
            f"# of coins available: {n}\n"
        )


@click.command(help="Mint a new reai nft, returns a LAUNCHER_ID.")
@click.option(
    "--fee",
    type=int,
    default=0,
    help="Transaction fee, defaults to 0",
)
@coro
@click.pass_context
async def mint(ctx, fee):
    wallet: ReaiWallet
    async with ctx.obj as wallet:
        debug("Minting a new coin for wallet: %s" % wallet.wallet_address)
        tx_id, launcher_id = await wallet.mint(fee=fee)
        debug("Got back tx_id: %s, launcher_id: %s" % (tx_id, launcher_id))
        if tx_id and launcher_id:
            click.echo(
                f"Minted a new reai nft with id: {launcher_id}\n\n"
                f"Track transaction: {tx_id}"
                f"\tFee: {fee} mojos"
                "\n\nNOTE: Store launcher_id somewhere safe as this wallet doesn't keep it anywhere yet.\n"
            )
        else:
            click.echo("Failed to mint for unknown reason.")


@click.command(
    name="add-pair",
    help="Add a pair of strings to coin data.\n\nPair will be prepended to the list, not appended. Only works on mutable coins.",
)
@click.option(
    "--fee",
    type=int,
    default=0,
    help="Transaction fee, defaults to 0",
)
@click.argument("launcher-id", callback=parse_launcher)
@click.argument("key", type=str)
@click.argument("value", type=str)
@coro
@click.pass_context
async def add_pair(ctx, launcher_id, key, value, fee):
    wallet: ReaiWallet
    async with ctx.obj as wallet:
        debug(
            f"Adding pair ({repr(key)}, {repr(value)}) to reai nft: {launcher_id.hex()}"
        )
        tx_id = await wallet.add_pair(launcher_id, (key, value), fee=fee)
        click.echo(f"Added pair ('{key}', '{value}') using transaction: {tx_id}")


@click.command(
    name="remove-pair",
    help="Remove a pair at a specifed index from coin data.\n\nOnly works on mutable coins.",
)
@click.option(
    "--fee",
    type=int,
    default=0,
    help="Transaction fee, defaults to 0",
)
@click.argument("launcher-id", callback=parse_launcher)
@click.argument("index", type=int)
@coro
@click.pass_context
async def remove_pair_at(ctx, launcher_id, index: int, fee: int):
    wallet: ReaiWallet
    async with ctx.obj as wallet:
        debug(f"Removing pair at index {index} from reai NFT: {launcher_id}")
        tx_id = await wallet.remove_pair_at(launcher_id, index, fee)
        click.echo(f"Removed pair at {index} using transaction: {tx_id}")


@click.command(name="freeze", help="Freezing makes the coin immutable")
@click.option(
    "--fee",
    type=int,
    default=0,
    help="Transaction fee, defaults to 0",
)
@click.argument("launcher-id", callback=parse_launcher)
@coro
@click.pass_context
async def freeze(ctx, launcher_id, fee):
    wallet: ReaiWallet
    async with ctx.obj as wallet:
        debug(f"Freezing reai nft: {launcher_id}")
        tx_id = await wallet.freeze(launcher_id, fee=fee)
        click.echo(f"Reai NFT frozen using transaction: {tx_id}")


@click.command(
    name="change-owner", help="Change the owner, works on mutable and immutable coins."
)
@click.option(
    "--fee",
    type=int,
    default=0,
    help="Transaction fee, defaults to 0",
)
@click.argument("launcher-id", callback=parse_launcher)
@click.argument("new-pub-key")
@coro
@click.pass_context
async def change_owner(ctx, launcher_id, new_pub_key, fee):
    wallet: ReaiWallet
    async with ctx.obj as wallet:
        debug(f"Changing ownership to {new_pub_key} on reai nft: {launcher_id}")
        tx_id = await wallet.set_ownership(launcher_id, new_pub_key, fee=fee)
        click.echo(f"Ownership changed to {new_pub_key} using transaction: {tx_id}")


@click.command(name="get-data")
@click.argument("launcher-id", callback=parse_launcher)
@coro
@click.pass_context
async def get_data(ctx, launcher_id):
    """Returns a JSON of coin data and metadata

    Can be piped into other commands."""
    wallet: ReaiWallet
    async with ctx.obj as wallet:
        debug(f"Fetching data for reai nft: {launcher_id.hex()}")
        data = await wallet.get_data(launcher_id)
        debug(f"Got back data: {data}")
        pretty_data = {
            "version": data[0],
            "data": [(i, x) for i, x in enumerate(data[1])],
        }

        class BytesDump(json.JSONEncoder):
            def default(self, obj):
                if isinstance(obj, bytes):
                    return obj.decode()
                return json.JSONEncoder.default(self, obj)

        click.echo(json.dumps(pretty_data, cls=BytesDump))


cli.add_command(mint)
cli.add_command(add_pair)
cli.add_command(remove_pair_at)
cli.add_command(change_owner)
cli.add_command(get_data)
cli.add_command(freeze)
cli.add_command(mint_k)
cli.add_command(get_number_of_available_coins)
cli.add_command(split_largest_coin_into_k)

if __name__ == "__main__":
    cli()
