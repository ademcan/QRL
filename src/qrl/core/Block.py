# coding=utf-8
# Distributed under the MIT software license, see the accompanying
# file LICENSE or http://www.opensource.org/licenses/mit-license.php.
from collections import OrderedDict

from google.protobuf.json_format import MessageToJson, Parse
from pyqrllib.pyqrllib import bin2hstr

from qrl.core import config
from qrl.core.misc import logger, ntp
from qrl.core.PoWValidator import PoWValidator
from qrl.core.Transaction import CoinBase, Transaction
from qrl.core.BlockHeader import BlockHeader
from qrl.crypto.misc import merkle_tx_hash
from qrl.generated import qrl_pb2


class Block(object):
    def __init__(self, protobuf_block=None):
        self._data = protobuf_block
        if protobuf_block is None:
            self._data = qrl_pb2.Block()

        self.blockheader = BlockHeader(self._data.header)

    @property
    def size(self):
        return self._data.ByteSize()

    @property
    def pbdata(self):
        """
        Returns a protobuf object that contains persistable data representing this object
        :return: A protobuf Block object
        :rtype: qrl_pb2.Block
        """
        return self._data

    @property
    def block_number(self):
        return self.blockheader.block_number

    @property
    def epoch(self):
        return int(self.block_number // config.dev.blocks_per_epoch)

    @property
    def headerhash(self):
        return self.blockheader.headerhash

    @property
    def prev_headerhash(self):
        return self.blockheader.prev_blockheaderhash

    @property
    def transactions(self):
        return self._data.transactions

    @property
    def mining_nonce(self):
        return self.blockheader.mining_nonce

    @property
    def PK(self):
        return self.blockheader.PK

    @property
    def block_reward(self):
        return self.blockheader.block_reward

    @property
    def fee_reward(self):
        return self.blockheader.fee_reward

    @property
    def timestamp(self):
        return self.blockheader.timestamp

    @property
    def mining_blob(self)->bytes:
        return self.blockheader.mining_blob

    @property
    def mining_nonce_offset(self)->bytes:
        return self.blockheader.nonce_offset

    @staticmethod
    def from_json(json_data):
        pbdata = qrl_pb2.Block()
        Parse(json_data, pbdata)
        return Block(pbdata)

    def verify_blob(self, blob: bytes) -> bool:
        return self.blockheader.verify_blob(blob)

    def set_nonces(self, mining_nonce, extra_nonce=0):
        self.blockheader.set_nonces(mining_nonce, extra_nonce)
        self._data.header.MergeFrom(self.blockheader.pbdata)

    def to_json(self)->str:
        # FIXME: Remove once we move completely to protobuf
        return MessageToJson(self._data)

    @staticmethod
    def create(block_number: int,
               prev_block_headerhash: bytes,
               prev_block_timestamp: int,
               transactions: list,
               miner_address: bytes):

        block = Block()

        # Process transactions
        hashedtransactions = []
        fee_reward = 0

        for tx in transactions:
            fee_reward += tx.fee

        # Prepare coinbase tx
        total_reward_amount = BlockHeader.block_reward_calc(block_number) + fee_reward
        coinbase_tx = CoinBase.create(total_reward_amount, miner_address, block_number)
        hashedtransactions.append(coinbase_tx.txhash)
        block._data.transactions.extend([coinbase_tx.pbdata])  # copy memory rather than sym link

        for tx in transactions:
            hashedtransactions.append(tx.txhash)
            block._data.transactions.extend([tx.pbdata])  # copy memory rather than sym link

        txs_hash = merkle_tx_hash(hashedtransactions)           # FIXME: Find a better name, type changes

        tmp_blockheader = BlockHeader.create(blocknumber=block_number,
                                             prev_block_headerhash=prev_block_headerhash,
                                             prev_block_timestamp=prev_block_timestamp,
                                             hashedtransactions=txs_hash,
                                             fee_reward=fee_reward)

        block.blockheader = tmp_blockheader

        block._data.header.MergeFrom(tmp_blockheader.pbdata)

        block.set_nonces(0, 0)

        return block

    def update_mining_address(self, mining_address: bytes):
        coinbase_tx = Transaction.from_pbdata(self.transactions[0])
        coinbase_tx.update_mining_address(mining_address)
        hashedtransactions = [coinbase_tx.txhash]

        for tx in self.transactions:
            hashedtransactions.append(tx.transaction_hash)

        self.blockheader.update_merkle_root(merkle_tx_hash(hashedtransactions))

        self._data.header.MergeFrom(self.blockheader.pbdata)

    def validate(self, state, future_blocks: OrderedDict) -> bool:
        if self.is_duplicate(state):
            logger.warning('Duplicate Block #%s %s', self.block_number, bin2hstr(self.headerhash))
            return False

        parent_block = state.get_block(self.prev_headerhash)

        # If parent block not found in state, then check if its in the future block list
        if not parent_block:
            try:
                parent_block = future_blocks[self.prev_headerhash]
            except KeyError:
                logger.warning('Parent block not found')
                logger.warning('Parent block headerhash %s', bin2hstr(self.prev_headerhash))
                return False

        if not self.validate_parent_child_relation(parent_block):
            logger.warning('Failed to validate blocks parent child relation')
            return False

        if not PoWValidator().validate_mining_nonce(state, self.blockheader):
            logger.warning('Failed PoW Validation')
            return False

        fee_reward = 0
        for index in range(1, len(self.transactions)):
            fee_reward += self.transactions[index].fee

        if len(self.transactions) == 0:
            return False

        try:
            coinbase_txn = Transaction.from_pbdata(self.transactions[0])
            coinbase_amount = coinbase_txn.amount

            if not coinbase_txn.validate_extended():
                return False

        except Exception as e:
            logger.warning('Exception %s', e)
            return False

        if not self.blockheader.validate(fee_reward, coinbase_amount):
            return False

        return True

    def apply_state_changes(self, address_txn) -> bool:
        coinbase_tx = Transaction.from_pbdata(self.transactions[0])

        if not coinbase_tx.validate_extended():
            logger.warning('Coinbase transaction failed')
            return False

        coinbase_tx.apply_state_changes(address_txn)

        len_transactions = len(self.transactions)
        for tx_idx in range(1, len_transactions):
            tx = Transaction.from_pbdata(self.transactions[tx_idx])

            if isinstance(tx, CoinBase):
                logger.warning('Found another coinbase transaction')
                return False

            if not tx.validate():
                return False

            addr_from_pk_state = address_txn[tx.addr_from]
            addr_from_pk = Transaction.get_slave(tx)
            if addr_from_pk:
                addr_from_pk_state = address_txn[addr_from_pk]

            if not tx.validate_extended(address_txn[tx.addr_from], addr_from_pk_state):
                return False

            expected_nonce = addr_from_pk_state.nonce + 1

            if tx.nonce != expected_nonce:
                logger.warning('nonce incorrect, invalid tx')
                logger.warning('subtype: %s', tx.type)
                logger.warning('%s actual: %s expected: %s', tx.addr_from, tx.nonce, expected_nonce)
                return False

            if addr_from_pk_state.ots_key_reuse(tx.ots_key):
                logger.warning('pubkey reuse detected: invalid tx %s', bin2hstr(tx.txhash))
                logger.warning('subtype: %s', tx.type)
                return False

            tx.apply_state_changes(address_txn)

        return True

    def is_duplicate(self, state) -> bool:
        if state.get_block(self.headerhash):
            return True

        return False

    def is_future_block(self) -> bool:
        if self.timestamp > ntp.getTime() + config.dev.block_max_drift:
            return True

        return False

    def validate_parent_child_relation(self, parent_block) -> bool:
        return self.blockheader.validate_parent_child_relation(parent_block)
