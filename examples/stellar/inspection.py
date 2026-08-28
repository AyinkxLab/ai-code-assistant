"""Read-only Stellar/Soroban examples.

These examples are safe to run: they only read public network data bound to
the configured Stellar network (testnet by default) and never sign, simulate,
or submit transactions. No keys or secrets are used.

Run from the repository root with the app configured:

    python examples/stellar/inspection.py

Set STELLAR_NETWORK (testnet/mainnet/futurenet) in the environment to change
the network. The RPC endpoints below use the project's validated presets.
"""

from __future__ import annotations


def demo_network_status() -> None:
    from app.services.stellar_inspection import network_status

    status = network_status()
    print("Configured network:", status["network"]["network"])
    print("RPC available:", status["rpc_available"])
    if status.get("latest_ledger"):
        print("Latest ledger:", status["latest_ledger"]["sequence"])


def demo_address_validation() -> None:
    from app.services.stellar import validate_stellar_address
    from app.services.stellar_xdr import validate_stellar_strkey

    # A real testnet account id (public, from the Stellar documentation).
    address = "GALAXYVOIDAOPZTDLHILAJQKCVVFMD4IKLXLSZV5YHO7VY74IWZILUTO"
    print("Structural G-address valid:", validate_stellar_address(address))
    print("Full strkey checksum valid:", validate_stellar_strkey(address, prefix="G")[0])


def demo_ledger_key_encoding() -> None:
    from app.services.stellar_xdr import ledger_key_for_account, ledger_key_for_contract

    address = "GALAXYVOIDAOPZTDLHILAJQKCVVFMD4IKLXLSZV5YHO7VY74IWZILUTO"
    contract = "CCPYZFKEAXHHS5VVW5J45TOU7S2EODJ7TZNJIA5LKDVL3PESCES6FNCI"
    print("Account LedgerKey:", ledger_key_for_account(address)[:24] + "…")
    print("Contract instance LedgerKey:", ledger_key_for_contract(contract)[:24] + "…")


def demo_inspect_account() -> None:
    from app.services.stellar_inspection import inspect_account

    address = "GALAXYVOIDAOPZTDLHILAJQKCVVFMD4IKLXLSZV5YHO7VY74IWZILUTO"
    try:
        result = inspect_account(address)
    except Exception as exc:  # network/account errors surface cleanly
        print("Account inspection unavailable:", exc)
        return
    print("Account:", result["address"], "sequence:", result["account"]["sequence"])
    print("Ledger freshness:", result["ledger_freshness"])


def main() -> None:
    demo_network_status()
    print()
    demo_address_validation()
    print()
    demo_ledger_key_encoding()
    print()
    demo_inspect_account()


if __name__ == "__main__":
    main()
