from base64 import b64decode
from hashlib import sha256

import algokit_utils
import pytest
from algokit_utils import (
    Account,
    TransactionParameters,
    TransferParameters,
    get_account,
    transfer,
)
from algokit_utils.config import config
from algosdk import abi
from algosdk.atomic_transaction_composer import (
    AccountTransactionSigner,
    TransactionWithSigner,
)
from algosdk.encoding import decode_address
from algosdk.error import AlgodHTTPError
from algosdk.transaction import PaymentTxn
from algosdk.v2client.algod import AlgodClient
from algosdk.v2client.indexer import IndexerClient

from smart_contracts.artifacts.app.game_client import GameClient


@pytest.fixture(scope="session")
def account(algod_client: AlgodClient) -> Account:
    account = get_account(algod_client, "ACCOUNT", fund_with_algos=100_000_000)
    print(account.address)
    return account


@pytest.fixture(scope="session")
def app_client(
    account: Account, algod_client: AlgodClient, indexer_client: IndexerClient
) -> GameClient:
    config.configure(
        debug=True,
        # trace_all=True,
    )
    print(account)
    client = GameClient(
        algod_client,
        creator=account,
        indexer_client=indexer_client,
    )

    client.deploy(
        on_schema_break=algokit_utils.OnSchemaBreak.AppendApp,
        on_update=algokit_utils.OnUpdate.AppendApp,
    )

    transfer(
        algod_client,
        TransferParameters(
            from_account=account,
            to_address=client.app_address,
            micro_algos=100_000_000,
        ),
    )

    return client


def test_register(
    algod_client: AlgodClient, app_client: GameClient, account: Account
) -> None:
    """Tests the `register` method."""
    box_abi = abi.ABIType.from_string("(uint64,string,uint64)")
    box_name = b"user" + decode_address(account.address)

    # Test application call return value
    user = app_client.register(
        name="Alice",
        transaction_parameters=TransactionParameters(boxes=[(0, box_name)]),
    ).return_value

    assert isinstance(user.registered_at, int) and user.registered_at > 0
    assert user.name == "Alice"
    assert isinstance(user.balance, int) and user.balance == 0

    # Test box value fetched from Algod
    box_value = b64decode(
        algod_client.application_box_by_name(app_client.app_id, box_name)["value"]
    )
    registered_at, name, balance = box_abi.decode(box_value)

    assert isinstance(registered_at, int) and registered_at == user.registered_at
    assert name == "Alice"
    assert isinstance(balance, int) and balance == 0


def test_fund_account(algod_client: AlgodClient, app_client: GameClient) -> None:
    """Tests the `fund_account` method."""
    # Generate new account
    account = get_account(app_client.algod_client, "test")
    app_client.signer = AccountTransactionSigner(account.private_key)

    box_abi = abi.ABIType.from_string("(uint64,string,uint64)")
    box_name = b"user" + decode_address(account.address)

    # Register a new user
    user = app_client.register(
        name="Bob",
        transaction_parameters=TransactionParameters(boxes=[(0, box_name)]),
    ).return_value

    # Store balance before funding
    balance_before = user.balance

    # Construct payment transaction
    ptxn = PaymentTxn(
        sender=account.address,
        sp=algod_client.suggested_params(),
        receiver=app_client.app_address,
        amt=10_000,
    )

    # Fund the user's account
    balance_returned = app_client.fund_account(
        payment=TransactionWithSigner(
            ptxn, AccountTransactionSigner(account.private_key)
        ),
        transaction_parameters=TransactionParameters(boxes=[(0, box_name)]),
    ).return_value

    # Test the value returned from the app call
    assert balance_before + 10_000 == balance_returned

    # Parse user's box from Algod
    box_value = b64decode(
        algod_client.application_box_by_name(app_client.app_id, box_name)["value"]
    )
    _, _, box_balance = box_abi.decode(box_value)

    # Test box value balance
    assert balance_before + 10_000 == box_balance


def test_admin_upsert_asset(
    algod_client: AlgodClient, app_client: GameClient, account: Account
) -> None:
    """Tests the `admin_upsert_asset` method."""
    # Switch back to creator account
    app_client.signer = AccountTransactionSigner(account.private_key)

    for asset in (
        ("POKEBALL", "Catches Pokemon", 200),
        ("POTION", "Restores 20 HP", 300),
        ("BICYCLE", "Allows you to travel faster", 1_000_000),
    ):
        name, _, _ = asset
        box_name = b"asset" + sha256(abi.StringType().encode(name)).digest()

        # Call app client
        app_client.admin_upsert_asset(
            asset=asset,
            transaction_parameters=TransactionParameters(boxes=[(0, box_name)]),
        )

        # Test box value fetched from Algod
        box_value = b64decode(
            algod_client.application_box_by_name(app_client.app_id, box_name)["value"]
        )
        box_abi = abi.ABIType.from_string("(string,string,uint64)")

        # Test box value balance
        assert asset == tuple(box_abi.decode(box_value))


def test_buy_asset(
    algod_client: AlgodClient, app_client: GameClient, account: Account
) -> None:
    """Tests the `buy_asset` method."""
    box = lambda name: b64decode(
        algod_client.application_box_by_name(app_client.app_id, name)["value"]
    )

    # Generate new account
    account = get_account(app_client.algod_client, "test_buyer")
    app_client.signer = AccountTransactionSigner(account.private_key)

    # Register new user
    user_box_name = b"user" + decode_address(account.address)
    app_client.register(
        name="Ash",
        transaction_parameters=TransactionParameters(boxes=[(0, user_box_name)]),
    )

    # Get asset price from box storage
    asset_name = "POKEBALL"
    asset_box_name = b"asset" + (
        asset_id := sha256(abi.StringType().encode(asset_name)).digest()
    )
    _, _, asset_price = abi.ABIType.from_string("(string,string,uint64)").decode(
        box(asset_box_name)
    )

    # Construct payment transaction to fund the user's game account
    ptxn = PaymentTxn(
        sender=account.address,
        sp=algod_client.suggested_params(),
        receiver=app_client.app_address,
        amt=asset_price * 2,
    )

    # Fund the user's account
    app_client.fund_account(
        payment=TransactionWithSigner(
            ptxn, AccountTransactionSigner(account.private_key)
        ),
        transaction_parameters=TransactionParameters(
            boxes=[(0, b"user" + decode_address(account.address))]
        ),
    )

    # Get user balance before buying asset
    user_box_abi = abi.ABIType.from_string("(uint64,string,uint64)")
    _, _, balance_before = user_box_abi.decode(box(user_box_name))
    user_asset_box_name = (
        b"user_asset" + sha256(decode_address(account.address) + asset_id).digest()
    )

    # Get user-asset quantity before buying
    try:
        quantity_before = abi.UintType(64).decode(box(user_asset_box_name))
    except AlgodHTTPError:
        quantity_before = 0

    buy = lambda: app_client.buy_asset(
        asset_id=asset_id,
        quantity=1,
        transaction_parameters=TransactionParameters(
            boxes=[
                (0, asset_box_name),
                (0, user_box_name),
                (0, user_asset_box_name),
            ]
        ),
    )

    # Buy one unit of asset
    buy()
    # Buy another unit of asset
    buy()

    # Get user balance after buying two units of the asset
    _, _, balance_after = user_box_abi.decode(box(user_box_name))

    # Test user balance in profile box
    assert balance_before - (asset_price * 2) == balance_after

    # Test user-asset box value
    quantity_after = abi.UintType(64).decode(box(user_asset_box_name))
    assert quantity_after - 2 == quantity_before
