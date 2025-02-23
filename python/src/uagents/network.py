"""Network and Contracts."""

import asyncio
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple, Union

from cosmpy.aerial.client import (
    DEFAULT_QUERY_INTERVAL_SECS,
    DEFAULT_QUERY_TIMEOUT_SECS,
    LedgerClient,
    NetworkConfig,
    prepare_and_broadcast_basic_transaction,
)
from cosmpy.aerial.contract import LedgerContract
from cosmpy.aerial.contract.cosmwasm import create_cosmwasm_execute_msg
from cosmpy.aerial.exceptions import NotFoundError, QueryTimeoutError
from cosmpy.aerial.faucet import FaucetApi
from cosmpy.aerial.tx import Transaction
from cosmpy.aerial.tx_helpers import TxResponse
from cosmpy.aerial.wallet import LocalWallet
from cosmpy.crypto.address import Address

from uagents.config import (
    ALMANAC_CONTRACT_VERSION,
    ALMANAC_REGISTRATION_WAIT,
    AVERAGE_BLOCK_INTERVAL,
    DEFAULT_LEDGER_TX_WAIT_SECONDS,
    MAINNET_CONTRACT_ALMANAC,
    MAINNET_CONTRACT_NAME_SERVICE,
    REGISTRATION_FEE,
    TESTNET_CONTRACT_ALMANAC,
    TESTNET_CONTRACT_NAME_SERVICE,
)
from uagents.crypto import Identity
from uagents.types import AgentEndpoint, AgentInfo, AgentNetwork
from uagents.utils import get_logger

logger = get_logger("network")


_faucet_api = FaucetApi(NetworkConfig.fetchai_stable_testnet())
_testnet_ledger = LedgerClient(NetworkConfig.fetchai_stable_testnet())
_mainnet_ledger = LedgerClient(NetworkConfig.fetchai_mainnet())


class InsufficientFundsError(Exception):
    """Raised when an agent has insufficient funds for a transaction."""


class AlmanacContractRecord(AgentInfo):
    contract_address: str
    sender_address: str
    timestamp: Optional[int] = None
    signature: Optional[str] = None

    def sign(self, identity: Identity):
        self.timestamp = int(time.time()) - ALMANAC_REGISTRATION_WAIT
        self.signature = identity.sign_registration(
            self.contract_address, self.timestamp, self.sender_address
        )


def get_ledger(network: AgentNetwork = "testnet") -> LedgerClient:
    """
    Get the Ledger client.

    Args:
        network (AgentNetwork, optional): The network to use. Defaults to "testnet".

    Returns:
        LedgerClient: The Ledger client instance.
    """
    if network == "mainnet":
        return _mainnet_ledger
    return _testnet_ledger


def get_faucet() -> FaucetApi:
    """
    Get the Faucet API instance.

    Returns:
        FaucetApi: The Faucet API instance.
    """
    return _faucet_api


def add_testnet_funds(wallet_address: str):
    """
    Add testnet funds to the provided wallet address.

    Args:
        wallet_address (str): The wallet address to add funds to.
    """
    _faucet_api._try_create_faucet_claim(  # pylint: disable=protected-access
        wallet_address
    )


def parse_record_config(
    record: Optional[Union[str, List[str], Dict[str, dict]]],
) -> Optional[List[Dict[str, Any]]]:
    """
    Parse the user-provided record configuration.

    Returns:
        Optional[List[Dict[str, Any]]]: The parsed record configuration in correct format.
    """
    if isinstance(record, dict):
        records = [
            {"address": val[0], "weight": val[1].get("weight") or 1}
            for val in record.items()
        ]
    elif isinstance(record, list):
        records = [{"address": val, "weight": 1} for val in record]
    elif isinstance(record, str):
        records = [{"address": record, "weight": 1}]
    else:
        records = None
    return records


async def wait_for_tx_to_complete(
    tx_hash: str,
    ledger: LedgerClient,
    timeout: Optional[timedelta] = None,
    poll_period: Optional[timedelta] = None,
) -> TxResponse:
    """
    Wait for a transaction to complete on the Ledger.

    Args:
        tx_hash (str): The hash of the transaction to monitor.
        ledger (LedgerClient): The Ledger client to poll.
        timeout (Optional[timedelta], optional): The maximum time to wait.
        the transaction to complete. Defaults to None.
        poll_period (Optional[timedelta], optional): The time interval to poll

    Returns:
        TxResponse: The response object containing the transaction details.
    """
    if timeout is None:
        timeout = timedelta(seconds=DEFAULT_QUERY_TIMEOUT_SECS)
    if poll_period is None:
        poll_period = timedelta(seconds=DEFAULT_QUERY_INTERVAL_SECS)
    start = datetime.now()
    while True:
        try:
            return ledger.query_tx(tx_hash)
        except NotFoundError:
            pass

        delta = datetime.now() - start
        if delta >= timeout:
            raise QueryTimeoutError()

        await asyncio.sleep(poll_period.total_seconds())


class AlmanacContract(LedgerContract):
    """
    A class representing the Almanac contract for agent registration.

    This class provides methods to interact with the Almanac contract, including
    checking if an agent is registered, retrieving the expiry height of an agent's
    registration, and getting the endpoints associated with an agent's registration.
    """

    def check_version(self) -> bool:
        """
        Check if the contract version supported by this version of uAgents matches the
        deployed version.

        Returns:
            bool: True if the contract version is supported, False otherwise.
        """
        try:
            deployed_version = self.get_contract_version()
            if deployed_version != ALMANAC_CONTRACT_VERSION:
                logger.warning(
                    f"The deployed version of the Almanac Contract is {deployed_version} "
                    f"and you are using version {ALMANAC_CONTRACT_VERSION}. "
                    "Update uAgents to the latest version to enable contract interactions.",
                )
                return False
        except Exception as e:
            logger.error(
                "Failed to query contract version. Contract interactions will be disabled."
            )
            logger.debug(e)
            return False
        return True

    def query_contract(self, query_msg: Dict[str, Any]) -> Any:
        """
        Execute a query with additional checks and error handling.

        Args:
            query_msg (Dict[str, Any]): The query message.

        Returns:
            Any: The query response.

        Raises:
            RuntimeError: If the contract address is not set or the query fails.
        """
        try:
            response = self.query(query_msg)
            if not isinstance(response, dict):
                raise ValueError("Invalid response format")
            return response
        except Exception as e:
            logger.error(f"Query failed with error: {e.__class__.__name__}.")
            logger.debug(e)
            raise

    def get_contract_version(self) -> str:
        """
        Get the version of the contract.

        Returns:
            str: The version of the contract.
        """
        query_msg = {"query_contract_state": {}}
        response = self.query_contract(query_msg)

        return response["contract_version"]

    def is_registered(self, address: str) -> bool:
        """
        Check if an agent is registered in the Almanac contract.

        Args:
            address (str): The agent's address.

        Returns:
            bool: True if the agent is registered, False otherwise.
        """
        query_msg = {"query_records": {"agent_address": address}}
        response = self.query_contract(query_msg)

        return bool(response.get("record"))

    def registration_needs_update(
        self,
        address: str,
        endpoints: List[AgentEndpoint],
        protocols: List[str],
        min_seconds_left: int,
    ) -> bool:
        """
        Check if an agent's registration needs to be updated.

        Args:
            address (str): The agent's address.
            endpoints (List[AgentEndpoint]): The agent's endpoints.
            protocols (List[str]): The agent's protocols.
            min_time_left (int): The minimum time left before the agent's registration expires

        Returns:
            bool: True if the agent's registration needs to be updated or will expire sooner
            than the specified minimum time, False otherwise.
        """
        seconds_to_expiry, registered_endpoints, registered_protocols = (
            self.query_agent_record(address)
        )
        return (
            not self.is_registered(address)
            or seconds_to_expiry < min_seconds_left
            or endpoints != registered_endpoints
            or protocols != registered_protocols
        )

    def query_agent_record(
        self, address: str
    ) -> Tuple[int, List[AgentEndpoint], List[str]]:
        """
        Get the records associated with an agent's registration.

        Args:
            address (str): The agent's address.

        Returns:
            Tuple[int, List[AgentEndpoint], List[str]]: The expiry height of the agent's
            registration, the agent's endpoints, and the agent's protocols.
        """
        query_msg = {"query_records": {"agent_address": address}}
        response = self.query_contract(query_msg)

        if not response.get("record"):
            return []

        if not response.get("record"):
            contract_state = self.query_contract({"query_contract_state": {}})
            expiry = contract_state.get("state", {}).get("expiry_height", 0)
            return expiry * AVERAGE_BLOCK_INTERVAL

        expiry_block = response["record"][0].get("expiry", 0)
        current_block = response.get("height", 0)

        seconds_to_expiry = (expiry_block - current_block) * AVERAGE_BLOCK_INTERVAL

        endpoints = []
        for endpoint in response["record"][0]["record"]["service"]["endpoints"]:
            endpoints.append(AgentEndpoint.model_validate(endpoint))

        protocols = response["record"][0]["record"]["service"]["protocols"]

        return seconds_to_expiry, endpoints, protocols

    def get_expiry(self, address: str) -> int:
        """
        Get the approximate seconds to expiry of an agent's registration.

        Args:
            address (str): The agent's address.

        Returns:
            int: The approximate seconds to expiry of the agent's registration.
        """
        return self.query_agent_record(address)[0]

    def get_endpoints(self, address: str) -> List[AgentEndpoint]:
        """
        Get the endpoints associated with an agent's registration.

        Args:
            address (str): The agent's address.

        Returns:
            List[AgentEndpoint]: The agent's registered endpoints.
        """
        return self.query_agent_record(address)[1]

    def get_protocols(self, address: str) -> List[str]:
        """
        Get the protocols associated with an agent's registration.

        Args:
            address (str): The agent's address.

        Returns:
            List[str]: The agent's registered protocols.
        """
        return self.query_agent_record(address)[2]

    def get_registration_msg(
        self,
        protocols: List[str],
        endpoints: List[AgentEndpoint],
        signature: str,
        sequence: int,
        address: str,
    ) -> Dict[str, Any]:
        return {
            "register": {
                "record": {
                    "service": {
                        "protocols": protocols,
                        "endpoints": [e.model_dump() for e in endpoints],
                    }
                },
                "signature": signature,
                "sequence": sequence,
                "agent_address": address,
            }
        }

    async def register(
        self,
        ledger: LedgerClient,
        wallet: LocalWallet,
        agent_address: str,
        protocols: List[str],
        endpoints: List[AgentEndpoint],
        signature: str,
        current_time: int,
    ):
        """
        Register an agent with the Almanac contract.

        Args:
            ledger (LedgerClient): The Ledger client.
            wallet (LocalWallet): The agent's wallet.
            agent_address (str): The agent's address.
            protocols (List[str]): List of protocols.
            endpoints (List[Dict[str, Any]]): List of endpoint dictionaries.
            signature (str): The agent's signature.
        """
        if not self.address:
            raise ValueError("Contract address not set")

        transaction = Transaction()

        almanac_msg = self.get_registration_msg(
            protocols=protocols,
            endpoints=endpoints,
            signature=signature,
            sequence=current_time,
            address=agent_address,
        )

        denom = self._client.network_config.fee_denomination
        transaction.add_message(
            create_cosmwasm_execute_msg(
                wallet.address(),
                self.address,
                almanac_msg,
                funds=f"{REGISTRATION_FEE}{denom}",
            )
        )

        transaction = prepare_and_broadcast_basic_transaction(
            ledger, transaction, wallet
        )
        timeout = timedelta(seconds=DEFAULT_LEDGER_TX_WAIT_SECONDS)
        await wait_for_tx_to_complete(transaction.tx_hash, ledger, timeout=timeout)

    async def register_batch(
        self,
        ledger: LedgerClient,
        wallet: LocalWallet,
        agent_records: List[AlmanacContractRecord],
    ):
        """
        Register multiple agents with the Almanac contract.

        Args:
            ledger (LedgerClient): The Ledger client.
            wallet (LocalWallet): The wallet of the registration sender.
            agents (List[ALmanacContractRecord]): The list of signed agent records to register.
        """
        if not self.address:
            raise ValueError("Contract address not set")

        transaction = Transaction()

        for record in agent_records:
            if record.timestamp is None:
                raise ValueError("Agent record is missing timestamp")

            if record.signature is None:
                raise ValueError("Agent record is not signed")

            almanac_msg = self.get_registration_msg(
                protocols=record.protocols,
                endpoints=record.endpoints,
                signature=record.signature,
                sequence=record.timestamp,
                address=record.address,
            )

            denom = self._client.network_config.fee_denomination
            transaction.add_message(
                create_cosmwasm_execute_msg(
                    wallet.address(),
                    self.address,
                    almanac_msg,
                    funds=f"{REGISTRATION_FEE}{denom}",
                )
            )

        transaction = prepare_and_broadcast_basic_transaction(
            ledger, transaction, wallet
        )
        timeout = timedelta(seconds=DEFAULT_LEDGER_TX_WAIT_SECONDS)
        await wait_for_tx_to_complete(transaction.tx_hash, ledger, timeout=timeout)

    def get_sequence(self, address: str) -> int:
        """
        Get the agent's sequence number for Almanac registration.

        Args:
            address (str): The agent's address.

        Returns:
            int: The agent's sequence number.
        """
        query_msg = {"query_sequence": {"agent_address": address}}
        sequence = self.query_contract(query_msg)["sequence"]

        return sequence


_mainnet_almanac_contract = AlmanacContract(
    None, _mainnet_ledger, Address(MAINNET_CONTRACT_ALMANAC)
)
_testnet_almanac_contract = AlmanacContract(
    None, _testnet_ledger, Address(TESTNET_CONTRACT_ALMANAC)
)


def get_almanac_contract(
    network: AgentNetwork = "testnet",
) -> Optional[AlmanacContract]:
    """
    Get the AlmanacContract instance.

    Args:
        network (AgentNetwork): The network to use. Defaults to "testnet".

    Returns:
        AlmanacContract: The AlmanacContract instance if version is supported.
    """
    if network == "mainnet" and _mainnet_almanac_contract.check_version():
        return _mainnet_almanac_contract
    if _testnet_almanac_contract.check_version():
        return _testnet_almanac_contract
    return None


class NameServiceContract(LedgerContract):
    """
    A class representing the NameService contract for managing domain names and ownership.

    This class provides methods to interact with the NameService contract, including
    checking name availability, checking ownership, querying domain public status,
    obtaining registration transaction details, and registering a name within a domain.
    """

    def query_contract(self, query_msg: Dict[str, Any]) -> Any:
        """
        Execute a query with additional checks and error handling.

        Args:
            query_msg (Dict[str, Any]): The query message.

        Returns:
            Any: The query response.

        Raises:
            ValueError: If the response from contract is not a dict.
        """
        try:
            response = self.query(query_msg)
            if not isinstance(response, dict):
                raise ValueError("Invalid response format")
            return response
        except Exception as e:
            logger.error(f"Querying NameServiceContract failed for query {query_msg}.")
            logger.debug(e)
            raise

    def is_name_available(self, name: str, domain: str) -> bool:
        """
        Check if a name is available within a domain.

        Args:
            name (str): The name to check.
            domain (str): The domain to check within.

        Returns:
            bool: True if the name is available, False otherwise.
        """
        query_msg = {"query_domain_record": {"domain": f"{name}.{domain}"}}
        return self.query_contract(query_msg)["is_available"]

    def is_owner(self, name: str, domain: str, wallet_address: str) -> bool:
        """
        Check if the provided wallet address is the owner of a name within a domain.

        Args:
            name (str): The name to check ownership for.
            domain (str): The domain to check within.
            wallet_address (str): The wallet address to check ownership against.

        Returns:
            bool: True if the wallet address is the owner, False otherwise.
        """
        query_msg = {
            "permissions": {
                "domain": f"{name}.{domain}",
                "owner": wallet_address,
            }
        }
        permission = self.query_contract(query_msg)["permissions"]
        return permission == "admin"

    def is_domain_public(self, domain: str) -> bool:
        """
        Check if a domain is public.

        Args:
            domain (str): The domain to check.

        Returns:
            bool: True if the domain is public, False otherwise.
        """
        res = self.query_contract(
            {"query_domain_flags": {"domain": domain.split(".")[-1]}}
        ).get("domain_flags")
        if res:
            return res["web3_flags"]["is_public"]
        return False

    def get_previous_records(self, name: str, domain: str):
        """
        Retrieve the previous records for a given name within a specified domain.

        Args:
            name (str): The name whose records are to be retrieved.
            domain (str): The domain within which the name is registered.

        Returns:
            A list of dictionaries, where each dictionary contains
            details of a record associated with the given name.
        """
        query_msg = {"query_domain_record": {"domain": f"{name}.{domain}"}}
        result = self.query_contract(query_msg)
        if result["record"] is not None:
            return result["record"]["records"][0]["agent_address"]["records"]
        return []

    def get_registration_tx(
        self,
        name: str,
        wallet_address: Address,
        agent_records: Union[List[Dict[str, Any]], str],
        domain: str,
        network: AgentNetwork,
    ):
        """
        Get the registration transaction for registering a name within a domain.

        Args:
            name (str): The name to be registered.
            wallet_address (str): The wallet address initiating the registration.
            agent_address (str): The address of the agent.
            domain (str): The domain in which the name is registered.
            test (bool): The agent type

        Returns:
            Optional[Transaction]: The registration transaction, or None if the name is not
            available or not owned by the wallet address.
        """
        transaction = Transaction()

        contract = Address(
            MAINNET_CONTRACT_NAME_SERVICE
            if network == "mainnet"
            else TESTNET_CONTRACT_NAME_SERVICE
        )

        if self.is_name_available(name, domain):
            price_per_second = self.query_contract({"query_contract_state": {}})[
                "price_per_second"
            ]
            amount = int(price_per_second["amount"]) * 86400
            denom = price_per_second["denom"]

            registration_msg = {"register": {"domain": f"{name}.{domain}"}}

            transaction.add_message(
                create_cosmwasm_execute_msg(
                    wallet_address, contract, registration_msg, funds=f"{amount}{denom}"
                )
            )
        elif not self.is_owner(name, domain, str(wallet_address)):
            return None

        record_msg = {
            "update_record": {
                "domain": f"{name}.{domain}",
                "agent_records": agent_records,
            }
        }

        transaction.add_message(
            create_cosmwasm_execute_msg(wallet_address, contract, record_msg)
        )

        return transaction

    async def register(
        self,
        ledger: LedgerClient,
        wallet: LocalWallet,
        agent_records: Optional[Union[str, List[str], Dict[str, dict]]],
        name: str,
        domain: str,
        overwrite: bool = True,
    ):
        """
        Register a name within a domain using the NameService contract.

        Args:
            ledger (LedgerClient): The Ledger client.
            wallet (LocalWallet): The wallet of the agent.
            agent_address (str): The address of the agent.
            name (str): The name to be registered.
            domain (str): The domain in which the name is registered.
            overwrite (bool, optional): Specifies whether to overwrite any existing
                addresses registered to the domain. If False, the address will be
                appended to the previous records. Defaults to True.
        """
        logger.info("Registering name...")
        chain_id = ledger.query_chain_id()
        network = (
            "mainnet"
            if chain_id == NetworkConfig.fetchai_mainnet().chain_id
            else "testnet"
        )

        records = parse_record_config(agent_records)
        if not records:
            raise ValueError("Invalid record configuration")
        agent_addresses = [val.get("address") for val in records]

        for agent_address in agent_addresses:
            if not get_almanac_contract(network).is_registered(
                agent_address  # type: ignore
            ):
                logger.warning(
                    "Address %s needs to be registered in almanac contract "
                    "to be registered in a domain.",
                    agent_address,
                )
                return

        if not self.is_domain_public(domain):
            logger.warning(
                f"Domain {domain} is not public, please select a public domain"
            )
            return

        if not overwrite:
            previous_records = self.get_previous_records(name, domain)
            records = list(
                {
                    f"{rec['address']}_{rec['weight']}": rec
                    for rec in previous_records + records
                }.values()
            )

        transaction = self.get_registration_tx(
            name,
            wallet.address(),
            records,
            domain,
            network=network,
        )

        if transaction is None:
            logger.error(
                f"Please select another name, {name} is owned by another address"
            )
            return
        transaction = prepare_and_broadcast_basic_transaction(
            ledger, transaction, wallet
        )
        await wait_for_tx_to_complete(transaction.tx_hash, ledger)
        logger.info("Registering name...complete")

    async def unregister(
        self,
        name: str,
        domain: str,
        wallet: LocalWallet,
    ):
        """
        Unregister a name within a domain using the NameService contract.

        Args:
            name (str): The name to be unregistered.
            domain (str): The domain in which the name is registered.
            wallet (LocalWallet): The wallet of the agent.
        """
        logger.info("Unregistering name...")

        if self.is_name_available(name, domain):
            logger.warning("Nothing to unregister... (name is not registered)")
            return

        msg = {
            "remove_domain": {
                "domain": f"{name}.{domain}",
            }
        }
        self.execute(msg, wallet).wait_to_complete()

        logger.info("Unregistering name...complete")


_mainnet_name_service_contract = NameServiceContract(
    None, _mainnet_ledger, Address(MAINNET_CONTRACT_NAME_SERVICE)
)
_testnet_name_service_contract = NameServiceContract(
    None, _testnet_ledger, Address(TESTNET_CONTRACT_NAME_SERVICE)
)


def get_name_service_contract(network: AgentNetwork = "testnet") -> NameServiceContract:
    """
    Get the NameServiceContract instance.

    Args:
        test (bool): Whether to use the testnet or mainnet. Defaults to True.

    Returns:
        NameServiceContract: The NameServiceContract instance.
    """
    if network == "mainnet":
        return _mainnet_name_service_contract
    return _testnet_name_service_contract
