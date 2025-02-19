import signal
import traceback
from pathlib import Path
from typing import List, Optional
import nft_storage
import asyncio
import logging

from nft_storage.api import nft_storage_api
from chia.rpc.full_node_rpc_client import FullNodeRpcClient
from chia.rpc.wallet_rpc_client import WalletRpcClient
from chia.types.coin_record import CoinRecord
from chia.util.bech32m import decode_puzzle_hash, encode_puzzle_hash
from chia.util.config import load_config
from chia.util.default_root import DEFAULT_ROOT_PATH
from chia.util.ints import uint16

from cfg import cfg
from db_api import DatabaseApi
from helpers import get_metadata_path, get_image_path, sha256sum

# Initialize logging
log = logging.getLogger(__name__)


class WalletServer:
    shut_down: bool
    shut_down_event: asyncio.Event
    full_node_rpc: FullNodeRpcClient
    wallet_client: WalletRpcClient
    receive_address: str
    database_api: DatabaseApi

    @staticmethod
    async def create_web_server():
        self = WalletServer()
        self.shut_down = False
        self.shut_down_event = asyncio.Event()
        config = load_config(DEFAULT_ROOT_PATH, "config.yaml")
        self_hostname = config["self_hostname"]
        wallet_rpc_port = config["wallet"]["rpc_port"]
        fullnode_rpc_port = config["full_node"]["rpc_port"]

        self.wallet_client = await WalletRpcClient.create("127.0.0.1", uint16(wallet_rpc_port), DEFAULT_ROOT_PATH, config)
        self.receive_address = cfg.wallet.receive_address
        self.full_node_rpc = await FullNodeRpcClient.create(
            self_hostname, fullnode_rpc_port, DEFAULT_ROOT_PATH, config
        )
        self.database_api = await DatabaseApi.create_api()
        asyncio.create_task(self.monitor_deposit_task())
        asyncio.create_task(self.payout_task())

        asyncio.get_running_loop().add_signal_handler(signal.SIGINT, self.stop_all)
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, self.stop_all)
        return self

    async def monitor_deposit_task(self):
        print("\U0001F916 Starting MinFT1 monitoring loop ==========================================")
        while True:
            address_bytes = decode_puzzle_hash(self.receive_address)
            coin_records: List[
                CoinRecord
            ] = await self.full_node_rpc.get_coin_records_by_puzzle_hash(
                address_bytes, False
            )
            print(f"\U00002139 Receive address has {len(coin_records)} coin(s).")

            for coin_record in coin_records:

                # Fetch parent spend
                parent_coin_name = coin_record.coin.parent_coin_info
                parent_coin: Optional[
                    CoinRecord
                ] = await self.full_node_rpc.get_coin_record_by_name(
                    parent_coin_name
                )

                received_amount = coin_record.coin.amount
                to_puzzle_hash = parent_coin.coin.puzzle_hash
                to_address = encode_puzzle_hash(to_puzzle_hash, "xch")

                parent_id = parent_coin.coin.name()
                task = await self.database_api.get_mint_task(parent_id.hex())
                if task is not None:
                    # print(f"\U00002716 Task for mint {task.mint_id} already exists.")
                    continue

                if received_amount < cfg.wallet.price_xch:
                    print(f"\U00002716 Not enough XCH received: {received_amount}")
                    continue

                print(f"\U0001f4B0 Received amount: {received_amount}")
                print(f"  Parent_id: {parent_id.hex()}")
                print(f"  Mint for: {to_puzzle_hash.hex()}")

                await self.database_api.create_mint_task(parent_id=parent_id.hex(), to_puzzle_hash=to_address)
                print("\U00002705 Creating new mint task")
                pass

            await asyncio.sleep(30)

    # mint
    async def mint(self, image_path: str, to_address: str, mint_id: int):
        sha = sha256sum(image_path)

        configuration = nft_storage.Configuration(
            host="https://api.nft.storage",
            access_token=cfg.key.nft_storage_api
        )

        # Store image

        image_cid = None

        with nft_storage.ApiClient(configuration) as api_client:
            # Create an instance of the API class
            api_instance = nft_storage_api.NFTStorageAPI(api_client)
            body = open(image_path, 'rb')  # file_type |

            try:
                api_response = api_instance.store(body, _check_return_type=False)
                image_cid = api_response["value"]["cid"]
                print(api_response)
            except nft_storage.ApiException as e:
                print("Exception when calling NFTStorageAPI->check: %s\n" % e)

        assert image_cid is not None

        image_url = f"https://{image_cid}.ipfs.nftstorage.link/"

        # Store metadata
        metadata_cid = None
        metadata_json_path = get_metadata_path(mint_id)

        with nft_storage.ApiClient(configuration) as api_client:
            # Create an instance of the API class
            api_instance = nft_storage_api.NFTStorageAPI(api_client)
            body = open(metadata_json_path, 'rb')  # file_type |

            try:
                api_response = api_instance.store(body, _check_return_type=False)
                metadata_cid = api_response["value"]["cid"]
                print(api_response)
            except nft_storage.ApiException as e:
                print("Exception when calling NFTStorageAPI->check: %s\n" % e)

        assert metadata_cid is not None

        metadata_url = f"https://{metadata_cid}.ipfs.nftstorage.link/"
        metadata_sha = sha256sum(metadata_json_path)

        # json_path.unlink()
        print("\U000026A0 Starting minting process!")
        print({
            'wallet_id': cfg.wallet.nft_wallet_id,
            'royalty_address': cfg.wallet.royalties_address,
            'target_address': to_address,
            'hash': sha,
            'uris': [image_url],
            'fee': cfg.wallet.minting_fees,
            'meta_hash': metadata_sha,
            'meta_uris': [metadata_url],
            'license_hash': cfg.collection.license_hash,
            'license_uris': [cfg.collection.license_url],
            'royalty_percentage': cfg.wallet.royalties_percent,
            'did_id': cfg.wallet.did
        })
        await self.wallet_client.mint_nft(
            wallet_id=cfg.wallet.nft_wallet_id,
            royalty_address=cfg.wallet.royalties_address,
            target_address=to_address,
            hash=sha,
            uris=[image_url],
            fee=cfg.wallet.minting_fees,
            meta_hash=metadata_sha,
            meta_uris=[metadata_url],
            license_hash=cfg.collection.license_hash,
            license_uris=[cfg.collection.license_url],
            royalty_percentage=cfg.wallet.royalties_percent,
            did_id=cfg.wallet.did
        )

    # chia wallet nft mint -f 518133150 -i 39 -ra xch16dnl4tzef59ahmum8cm65es8kxslpgacey2prxdwh44ha6dj6lnsazm4ne -ta xch1qnpxe232hr40hl0kgw5n6tqsldyy68tdlc7zfxx88aw3uz026wlsjk9xs2 -mu https://bafkreibnqu7h4dkzuuhyptneo3fhw2trretgfpuscf4dc6uwgnxy556e54.ipfs.nftstorage.link -mh 2d853e7e0d59a50f87cda476ca7b6a71892662be921178317a96336f8ef7c4ef -u https://bafybeicn4prwjotrze6ofzrk3ssat4avqlgjf6wp5rsaudgdysvtzr55ie.ipfs.nftstorage.link -nh 1952b61f21f5cb2b91beaeb01fd53f1baefeed76b1e88beb73ff56a9b93b25fc -lu https://bafkreicvmmvbnri62bde6qkuicrlloauavgehdvtmo5j5t3xhhu5dheije.ipfs.nftstorage.link -lh 55632a16c51ed0464f415440a2b5b814054c438eb363ba9ecf7739e9d19c8849 -rp 500 -m 0.00001
    async def payout_task(self):
        while True:
            await asyncio.sleep(10)
            standard = await self.wallet_client.get_wallet_balance(1)
            standard_balance = standard["spendable_balance"]
            standard_total = standard["confirmed_wallet_balance"]

            mint = False
            if standard_balance > 0 and standard_balance == standard_total:
                mint = True

            if mint is False:
                continue

            # Get pending mints
            tasks = await self.database_api.get_pending_tasks()

            if len(tasks) == 0:
                # print(f"There are {len(tasks)} tasks")
                continue

            print(f"\U00002139 {len(tasks)} tasks in queue.")

            tasks = await self.database_api.get_pending_tasks()
            task = tasks[0]

            if task.mint_image_url is not None and task.mint_image_url != "":
                image_path = task.mint_image_url
            else:
                image_path = get_image_path(task.mint_id)

            assert Path(image_path).is_file()
            assert len(Path(image_path).read_bytes()) > 10000

            # new_path = Path(f"{Path(image_path).parent}/{task.mint_id}.png")
            # new_path.write_bytes(Path(image_path).read_bytes())

            new_path_str = f"{Path(image_path).absolute()}"
            if task.mint_id+1 >= cfg.collection.size:
                print(f"\U00002139 This collection is fully minted. Minting is disabled. ")
                await asyncio.sleep(30000)
                continue

            print(f"\U00002705 Time to mint {task}")

            await task.update(status=1).apply()
            await self.mint(to_address=task.to_address, mint_id=task.mint_id, image_path=new_path_str)

    def stop_all(self):
        self.shut_down = True
        self.shut_down_event.set()


async def run_wallet_server():
    server: WalletServer = await WalletServer.create_web_server()
    await server.shut_down_event.wait()


def main():
    asyncio.run(run_wallet_server())


if __name__ == "__main__":
    try:
        main()
    except Exception:
        tb = traceback.format_exc()
        print(f"Error in Web Server. {tb}")
        raise
