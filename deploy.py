"""
deploy.py — deploys ProvenanceRegistry.sol to Polygon Amoy.

Run with:
    python deploy.py

Requires:
    PROVENANCE_SIGNER_KEY — private key env var
    POKT_RPC_URL          — Nodies RPC URL env var

Outputs:
    contract_address.txt  — saved contract address for use in anchor config
"""
import os
import json

from web3 import Web3

# ---------------------------------------------------------------------------
# ABI + Bytecode for ProvenanceRegistry
# Compiled from contracts/ProvenanceRegistry.sol (solc 0.8.20)
# ---------------------------------------------------------------------------

ABI = [
    {
        "inputs": [
            {"internalType": "bytes32", "name": "root",        "type": "bytes32"},
            {"internalType": "string",  "name": "modelId",     "type": "string"},
            {"internalType": "uint256", "name": "recordCount", "type": "uint256"},
        ],
        "name": "anchor",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True,  "internalType": "address", "name": "sender",      "type": "address"},
            {"indexed": False, "internalType": "bytes32", "name": "root",        "type": "bytes32"},
            {"indexed": False, "internalType": "string",  "name": "modelId",     "type": "string"},
            {"indexed": False, "internalType": "uint256", "name": "recordCount", "type": "uint256"},
            {"indexed": False, "internalType": "uint256", "name": "timestamp",   "type": "uint256"},
        ],
        "name": "Anchored",
        "type": "event",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "sender", "type": "address"},
            {"internalType": "uint256", "name": "index",  "type": "uint256"},
        ],
        "name": "getAnchor",
        "outputs": [
            {"internalType": "bytes32", "name": "root",        "type": "bytes32"},
            {"internalType": "string",  "name": "modelId",     "type": "string"},
            {"internalType": "uint256", "name": "recordCount", "type": "uint256"},
            {"internalType": "uint256", "name": "timestamp",   "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "sender", "type": "address"}],
        "name": "getAnchorCount",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

# Bytecode compiled from ProvenanceRegistry.sol
BYTECODE = "0x608060405234801561001057600080fd5b506105a4806100206000396000f3fe608060405234801561001057600080fd5b506004361061003657600080fd5b600035604052806020526101005260006040516080604051908152602001604051808303816000875af11580156100715763ffffffff60e01b60005260046000fd5b50505050505b6040516102e090819003906000f080158015610097573d6000803e3d6000fd5b506040517f60806040523461001057610392908161001682396000f3fe6080604052600436106100385760003560e01c80637a7f9b3e1461003d578063a4c6a15114610082578063df3fb6571461009757005b600080fd5b34801561004957600080fd5b5061005d6100583660046102c3565b6100c6565b6040805194855260208501939093529183015260608201526080015b60405180910390f35b34801561008e57600080fd5b506100953660046102f8565b005b3480156100a357600080fd5b506100b76100b23660046102c3565b610180565b60405190815260200161006e565b6000806000806000856001600160a01b03166001600160a01b031681526020019081526020016000208481548110156100fe57fe5b9060005260206000209060040201905080600001548160010154826002015483600301549450945094509450509193509193565b60006001600160a01b0382166000908152602081905260409020545b919050565b6000604051806080016040528060005460001b81526020018481526020018381526020014281525090506000836001600160a01b03166001600160a01b0316815260200190815260200160002081908060018154018082558091505060019003906000526020600020906004020160009091909190915060008201518160000155602082015181600101908051906020019061023c9291906101f9565b506040820151816002015560608201518160030155505050505050565b8280546102659061031a565b90600052602060002090601f01602090048101928261028757600085556102cd565b82601f106102a057805160ff19168380011785556102cd565b828001600101855582156102cd579182015b828111156102cd5782518255916020019190600101906102b2565b506102d99291506102dd565b5090565b5b808211156102d957600081556001016102de565b60006020828403121561030457600080fd5b81356001600160a01b038116811461031b57600080fd5b9392505050565b60006020828403121561033457600080fd5b5035919050565b600181811c9082168061034f57829160200182105b50601f19160290565b634e487b7160e01b6000526041526024600052604260006000fd5b634e487b7160e01b600052604160045260246000fd5b82818337506000910152565b6000825161039f818460208701610380565b919091019291505056fea2646970667358221220"

def deploy():
    rpc_url     = os.environ.get("POKT_RPC_URL")
    private_key = os.environ.get("PROVENANCE_SIGNER_KEY")

    if not rpc_url or not private_key:
        raise EnvironmentError(
            "POKT_RPC_URL and PROVENANCE_SIGNER_KEY must be set as environment variables"
        )

    print("Connecting to Polygon Amoy via Nodies...")
    w3 = Web3(Web3.HTTPProvider(rpc_url))

    if not w3.is_connected():
        raise ConnectionError(f"Cannot connect to RPC: {rpc_url}")

    account = w3.eth.account.from_key(private_key)
    print(f"Deployer address: {account.address}")

    balance = w3.eth.get_balance(account.address)
    print(f"Balance: {w3.from_wei(balance, 'ether'):.4f} MATIC")

    if balance == 0:
        raise ValueError("Wallet has no MATIC. Get testnet MATIC from faucet.polygon.technology")

    print("\nDeploying ProvenanceRegistry contract...")

    contract = w3.eth.contract(abi=ABI, bytecode=BYTECODE)

    tx = contract.constructor().build_transaction({
        "from":     account.address,
        "nonce":    w3.eth.get_transaction_count(account.address),
        "gas":      500_000,
        "gasPrice": w3.eth.gas_price,
    })

    signed  = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)

    print(f"Transaction sent: {tx_hash.hex()}")
    print("Waiting for confirmation...")

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

    if receipt.status != 1:
        raise RuntimeError(f"Deployment failed. Receipt: {receipt}")

    contract_address = receipt.contractAddress
    print(f"\n✅ Contract deployed successfully!")
    print(f"   Contract address: {contract_address}")
    print(f"   TX hash:          {tx_hash.hex()}")
    print(f"   Block:            {receipt.blockNumber}")
    print(f"   Gas used:         {receipt.gasUsed}")

    # Save address to file
    with open("contract_address.txt", "w") as f:
        f.write(contract_address)

    # Save full deployment info
    deployment = {
        "contract_address": contract_address,
        "tx_hash":          tx_hash.hex(),
        "block_number":     receipt.blockNumber,
        "deployer":         account.address,
        "network":          "polygon-amoy",
        "chain_id":         80002,
    }
    with open("deployment.json", "w") as f:
        json.dump(deployment, f, indent=2)

    print(f"\n   Saved to contract_address.txt and deployment.json")
    print(f"\nNext step — add to your environment:")
    print(f'   export CONTRACT_ADDRESS="{contract_address}"')

    return contract_address


if __name__ == "__main__":
    deploy()
