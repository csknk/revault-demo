import bitcoin.rpc
import threading
import time

from bip32 import BIP32
from bitcoin.core import lx, COIN
from bitcoin.wallet import CBitcoinAddress, CKey
from decimal import Decimal
from .bitcoindapi import BitcoindApi
from .cosigningapi import CosigningApi
from .serverapi import ServerApi
from .transactions import (
    vault_txout, emergency_txout, create_emergency_vault_tx,
    sign_emergency_vault_tx, form_emergency_vault_tx, create_unvault_tx,
    sign_unvault_tx, form_unvault_tx, create_cancel_tx, sign_cancel_tx,
    form_cancel_tx, create_emer_unvault_tx, sign_emer_unvault_tx,
    form_emer_unvault_tx, create_spend_tx, sign_spend_tx, form_spend_tx,
    ALL_ANYONECANPAY
)
from .utils import tx_feerate, bump_feerate


class Vault:
    """The vault from the viewpoint of one of the stakeholders.

    Allows to derive the next key of the HD wallets of all the stakeholders, to
    deterministically derive each vault.
    Builds and signs all the necessary transactions when spending from the
    vault.

    Note that this demo doesn't comport some components of the whole
    architecture, for example there is no interaction with a watchtower
    (including the update of transactions and signatures as feerate moves).
    """
    def __init__(self, xpriv, xpubs, emergency_pubkeys, bitcoin_conf_path,
                 cosigning_url, sigserver_url, acked_addresses,
                 current_index=0, birthdate=None):
        """
        We need the xpub of all the other stakeholders to derive their pubkeys.

        :param xpriv: Who am I ? Has to correspond to one of the following
                      xpub. As str.
        :param xpubs: A list of the xpub of all the stakeholders (as str), in
                      the following order: 1) first trader 2) second trader
                      3) first "normie" stakeholder 4) second "normie"
                      stakeholder.
        :param emergency_pubkeys: A list of the four offline keys of the
                                  stakeholders, as bytes.
        :param bitcoin_conf_path: Path to bitcoin.conf.
        :param cosigning_url: The url of the cosigning server.
        :param sigserver_url: The url of the server to post / get the sigs from
                              other stakeholders.
        :param acked_addresses: Addresses to which we accept to spend.
        :param birthdate: The timestamp at which this wallet has been created.
                          If not passed, will assume newly-created wallet.
        """
        assert len(xpubs) == 4
        self.our_bip32 = BIP32.from_xpriv(xpriv)
        self.keychains = []
        for xpub in xpubs:
            if xpub != self.our_bip32.get_master_xpub():
                self.keychains.append(BIP32.from_xpub(xpub))
            else:
                self.keychains.append(None)
        self.all_xpubs = xpubs
        self.emergency_pubkeys = emergency_pubkeys
        # Ok, shitload of indexes. The current one is the lower bound of the
        # range we will import to bitcoind as watchonly. The max one is the
        # upper bond, the current "gen" one is to generate new addresses.
        self.current_index = current_index
        self.current_gen_index = self.current_index
        self.max_index = current_index + 500
        self.index_treshold = self.max_index

        self.birthdate = int(time.time()) if birthdate is None else birthdate

        self.bitcoind = BitcoindApi(bitcoin_conf_path)

        # First of all, watch the emergency vault
        self.watch_emergency_vault()
        # And store the corresponding address..
        txo = emergency_txout(self.emergency_pubkeys, 0)
        self.emergency_address = str(CBitcoinAddress
                                     .from_scriptPubKey(txo.scriptPubKey))

        # The cosigning server, asked for its signature for the spend_tx
        self.cosigner = CosigningApi(cosigning_url)
        self.cosigner_pubkey = self.cosigner.get_pubkey()

        # The "sig" server, used to store and exchange signatures between
        # vaults and which provides us a feerate.
        # Who am I ?
        stk_id = self.keychains.index(None) + 1
        self.sigserver = ServerApi(sigserver_url, stk_id)

        self.vault_addresses = []
        self.unvault_addresses = []
        self.update_watched_addresses()

        # We keep track of each vault, see below when we fill it for details
        # about what it contains. Basically all the transactions, the
        # signatures and some useful fields (like "are all txs signed ?").
        self.vaults = []
        self.vaults_lock = threading.Lock()

        # Poll for funds until we die
        self.funds_poller_stop = threading.Event()
        self.funds_poller = threading.Thread(target=self.poll_for_funds,
                                             daemon=True)
        self.funds_poller.start()

        # Poll for spends until we die
        self.acked_addresses = acked_addresses
        self.acked_spends = []
        self.spends_poller_stop = threading.Event()
        self.spends_poller = threading.Thread(target=self.poll_for_spends,
                                              daemon=True)
        self.spends_poller.start()

        # Don't start polling for signatures just yet, we don't have any vault!
        self.update_sigs_stop = threading.Event()
        self.update_sigs_thread =\
            threading.Thread(target=self.update_all_signatures, daemon=True)

        self.stopped = False

    def __del__(self):
        if not self.stopped:
            self.stop()

    def stop(self):
        # Stop the thread polling bitcoind
        self.funds_poller_stop.set()
        self.funds_poller.join()
        self.bitcoind.close()

        # The two threads polling the server will stop by themselves

        self.stopped = True

    def get_pubkeys(self, index):
        """Get all the pubkeys for this {index}.

        :return: A list of the four pubkeys for this bip32 derivation index.
        """
        pubkeys = []
        for keychain in self.keychains:
            if keychain:
                pubkeys.append(keychain.get_pubkey_from_path([index]))
            else:
                pubkeys.append(self.our_bip32.get_pubkey_from_path([index]))
        return pubkeys

    def watch_emergency_vault(self):
        """There is only one emergency script"""
        script = emergency_txout(self.emergency_pubkeys, COIN).scriptPubKey
        addr = CBitcoinAddress.from_scriptPubKey(script)
        self.bitcoind.importaddress(str(addr), "revault_emergency")

    def update_watched_addresses(self):
        """Update the watchonly addresses"""
        # Which addresses should we look for when polling bitcoind ?
        for i in range(self.current_index, self.max_index):
            pubkeys = self.get_pubkeys(i)
            txo = vault_txout(pubkeys, 0)
            addr = str(CBitcoinAddress.from_scriptPubKey(txo.scriptPubKey))
            if addr not in self.vault_addresses:
                self.vault_addresses.append(addr)
        # Which addresses should bitcoind look for when polling the utxo set ?
        self.bitcoind.importmultiextended(self.all_xpubs, self.birthdate,
                                          self.current_index, self.max_index)

    def get_vault_address(self, index):
        """Get the vault address for index {index}"""
        pubkeys = self.get_pubkeys(index)
        txo = vault_txout(pubkeys, 0)
        return str(CBitcoinAddress.from_scriptPubKey(txo.scriptPubKey))

    def getnewaddress(self):
        """Get the next vault address, we bump the derivation index.

        :return: (str) The next vault address.
        """
        addr = self.get_vault_address(self.current_gen_index)
        # FIXME: This is too simplistic
        self.current_gen_index += 1
        # Mind the gap ! https://www.youtube.com/watch?v=UOPyGKDQuRk
        if self.current_gen_index > self.index_treshold - 20:
            self.update_watched_addresses()
        return addr

    def guess_index(self, vault_address):
        """Guess the index used to derive the 4 pubkeys used in this 4of4.

        :param vault_address: (str) The vault P2WSH address.

        :return: The index.
        """
        for index in range(self.max_index):
            if vault_address == self.get_vault_address(index):
                return index
        return None

    def get_vault_from_unvault(self, txid):
        """Get the vault corresponding to this unvault transaction."""
        for v in self.vaults:
            if v["unvault_tx"].GetTxid() == lx(txid):
                return v
        return None

    def watch_unvault(self, vault):
        """Import the address of this vault's unvault tx to bitcoind."""
        assert len(vault["unvault_tx"].vout) == 1
        addr = str(CBitcoinAddress.from_scriptPubKey(
            vault["unvault_tx"].vout[0].scriptPubKey
        ))
        if addr not in self.unvault_addresses:
            self.unvault_addresses.append(addr)
        self.bitcoind.importaddress(addr, "unvault", False)

    def create_sign_emergency(self, vault):
        """Create and return our signature for the vault emergency tx."""
        # Dummy amount to get the feerate..
        amount = bitcoin.core.COIN
        dummy_tx = create_emergency_vault_tx(lx(vault["txid"]), vault["vout"],
                                             amount, self.emergency_pubkeys)
        feerate = self.sigserver.get_feerate("emergency",
                                             dummy_tx.GetTxid().hex())
        fees = feerate * self.bitcoind.tx_size(dummy_tx)
        amount = vault["amount"] - fees
        vault["emergency_tx"] = \
            create_emergency_vault_tx(lx(vault["txid"]), vault["vout"],
                                      amount, self.emergency_pubkeys)
        # We onced used SIGHASH_SINGLE, so I added that. But let's keep it :
        # this assumption still holds.
        assert (len(vault["emergency_tx"].vin)
                == len(vault["emergency_tx"].vout) == 1)

        # Sign the one we keep with ALL..
        sig = sign_emergency_vault_tx(vault["emergency_tx"], vault["privkey"],
                                      vault["pubkeys"], vault["amount"],
                                      sign_all=True)
        vault["emergency_sigs"][self.keychains.index(None)] = sig
        # .. And the one we share with ALL | ANYONECANPAY
        return sign_emergency_vault_tx(vault["emergency_tx"], vault["privkey"],
                                       vault["pubkeys"], vault["amount"])

    def create_sign_unvault(self, vault):
        """Create and return our signature for the unvault tx."""
        dummy_amount = bitcoin.core.COIN
        dummy_tx = create_unvault_tx(lx(vault["txid"]), vault["vout"],
                                     vault["pubkeys"], self.cosigner_pubkey,
                                     dummy_amount)
        feerate = self.sigserver.get_feerate("cancel",
                                             dummy_tx.GetTxid().hex())
        tx_size = self.bitcoind.tx_size(dummy_tx)
        unvault_amount = vault["amount"] - feerate * tx_size
        # We reuse the vault pubkeys for the unvault script
        vault["unvault_tx"] = \
            create_unvault_tx(lx(vault["txid"]), vault["vout"],
                              vault["pubkeys"], self.cosigner_pubkey,
                              unvault_amount)
        return sign_unvault_tx(vault["unvault_tx"], vault["privkey"],
                               vault["pubkeys"], vault["amount"])

    def create_sign_cancel(self, vault):
        """Create and return our signature for the unvault cancel tx."""
        unvault_txid = vault["unvault_tx"].GetTxid()
        dummy_amount = bitcoin.core.COIN
        unvault_amount = vault["unvault_tx"].vout[0].nValue
        assert len(vault["unvault_tx"].vout) == 1
        # We make the cancel_tx pay to the same script, for simplicity
        dummy_tx = create_cancel_tx(unvault_txid, 0, vault["pubkeys"],
                                    dummy_amount)
        feerate = self.sigserver.get_feerate("cancel",
                                             dummy_tx.GetTxid().hex())
        tx_size = self.bitcoind.tx_size(dummy_tx)
        cancel_amount = unvault_amount - feerate * tx_size
        vault["cancel_tx"] = create_cancel_tx(unvault_txid, 0,
                                              vault["pubkeys"], cancel_amount)
        # We onced used SIGHASH_SINGLE, so I added that. But let's keep it :
        # this assumption still holds.
        assert (len(vault["cancel_tx"].vin)
                == len(vault["cancel_tx"].vout) == 1)

        # It wants the pubkeys for the prevout script, but they are the same!
        sig = sign_cancel_tx(vault["cancel_tx"], vault["privkey"],
                             vault["pubkeys"], self.cosigner_pubkey,
                             unvault_amount, sign_all=True)
        vault["cancel_sigs"][self.keychains.index(None)] = sig

        # Sign the one we share with ALL | ANYONECANPAY
        return sign_cancel_tx(vault["cancel_tx"], vault["privkey"],
                              vault["pubkeys"], self.cosigner_pubkey,
                              unvault_amount)

    def create_sign_unvault_emer(self, vault):
        """Create and return our signature for the unvault emergency tx."""
        unvault_txid = vault["unvault_tx"].GetTxid()
        dummy_amount = bitcoin.core.COIN
        unvault_amount = vault["unvault_tx"].vout[0].nValue
        # Last one, the emergency_tx
        dummy_tx = create_emer_unvault_tx(unvault_txid, 0,
                                          self.emergency_pubkeys, dummy_amount)
        feerate = self.sigserver.get_feerate("emergency",
                                             dummy_tx.GetTxid().hex())
        tx_size = self.bitcoind.tx_size(dummy_tx)
        emer_amount = unvault_amount - feerate * tx_size
        vault["unvault_emer_tx"] = \
            create_emer_unvault_tx(unvault_txid, 0, self.emergency_pubkeys,
                                   emer_amount)
        # We onced used SIGHASH_SINGLE, so I added that. But let's keep it :
        # this assumption still holds.
        assert (len(vault["unvault_emer_tx"].vin)
                == len(vault["unvault_emer_tx"].vout) == 1)

        # Sign the one we keep with ALL..
        sig = sign_emer_unvault_tx(vault["unvault_emer_tx"], vault["privkey"],
                                   vault["pubkeys"], self.cosigner_pubkey,
                                   unvault_amount)
        vault["unvault_emer_sigs"][self.keychains.index(None)] = sig
        # .. And the one we share with ALL | ANYONECANPAY
        return sign_emer_unvault_tx(vault["unvault_emer_tx"], vault["privkey"],
                                    vault["pubkeys"], self.cosigner_pubkey,
                                    unvault_amount)

    def get_signed_emergency_tx(self, vault):
        """Form and return the emergency transaction for this vault.

        This is where we can bump the fees and re-sign it if necessary.

        :return: The signed emergency transaction, or None if we did not
                 gathered all the signatures for it.
        """
        if None in vault["emergency_sigs"]:
            return None

        feerate = tx_feerate(self.bitcoind, vault["emergency_tx"])
        minimal_feerate = self.bitcoind.getfeerate("emergency")
        if feerate < minimal_feerate:
            sigs = vault["emergency_sigs"].copy()
            # Replace the ALL signature with a ALL|ANYONECANPAY one..
            sig = sign_emergency_vault_tx(vault["emergency_tx"],
                                          vault["privkey"], vault["pubkeys"],
                                          vault["amount"])
            sigs[self.keychains.index(None)] = sig
            # Form the transaction..
            tx = form_emergency_vault_tx(vault["emergency_tx"],
                                         vault["pubkeys"], sigs)
            # And finally amend it and sign it with ALL
            return bump_feerate(self.bitcoind, tx, minimal_feerate - feerate)

        # No need to bump the fees, keep the ALL signature
        return form_emergency_vault_tx(vault["emergency_tx"],
                                       vault["pubkeys"],
                                       vault["emergency_sigs"])

    def get_signed_unvault_tx(self, vault):
        """Form and return the unvault transaction for this {vault}.

        :return: The signed unvault transaction, or None if we did not
                 gathered all the signatures for it.
        """
        if None in vault["unvault_sigs"]:
            return None

        return form_unvault_tx(vault["unvault_tx"], vault["pubkeys"],
                               vault["unvault_sigs"])

    def get_signed_cancel_tx(self, vault):
        """Form and return the cancel transaction for this vault's unvault.

        This is where we can bump the fees and re-sign it if necessary.

        :return: The signed transaction, or None if we did not gathered all
                 the signatures for it yet.
        """
        if None in vault["cancel_sigs"]:
            return None

        unvault_amount = vault["unvault_tx"].vout[0].nValue
        feerate = tx_feerate(self.bitcoind, vault["cancel_tx"], unvault_amount)
        minimal_feerate = self.bitcoind.getfeerate("cancel")
        if feerate < minimal_feerate:
            sigs = vault["cancel_sigs"].copy()
            # Replace the ALL signature by a ALL|ANYONECANPAY one..
            sig = sign_cancel_tx(vault["cancel_tx"], vault["privkey"],
                                 vault["pubkeys"], self.cosigner_pubkey,
                                 unvault_amount)
            sigs[self.keychains.index(None)] = sig
            # Form the transaction..
            tx = form_cancel_tx(vault["cancel_tx"], sigs,
                                vault["pubkeys"], self.cosigner_pubkey)
            # And finally amend it and sign it with ALL
            return bump_feerate(self.bitcoind, tx, minimal_feerate - feerate,
                                unvault_amount)

        # No need to bump the fees, keep the ALL signature
        return form_cancel_tx(vault["cancel_tx"], vault["cancel_sigs"],
                              vault["pubkeys"], self.cosigner_pubkey)

    def get_signed_unvault_emergency_tx(self, vault):
        """Form and return the emergency transaction for this vault's unvault.

        This is where we can bump the fees and re-sign it if necessary.

        :return: The signed transaction, or None if we did not gathered all
                 the signatures for it yet.
        """
        if None in vault["unvault_emer_sigs"]:
            return None

        unvault_amount = vault["unvault_tx"].vout[0].nValue
        feerate = tx_feerate(self.bitcoind, vault["unvault_emer_tx"],
                             unvault_amount)
        minimal_feerate = self.bitcoind.getfeerate("cancel")
        if feerate < minimal_feerate:
            sigs = vault["unvault_emer_sigs"].copy()
            # Replace the ALL signature by a ALL|ANYONECANPAY one..
            sig = sign_emer_unvault_tx(vault["unvault_emer_tx"],
                                       vault["privkey"], vault["pubkeys"],
                                       self.cosigner_pubkey, unvault_amount)
            sigs[self.keychains.index(None)] = sig
            # Form the transaction..
            tx = form_emer_unvault_tx(vault["unvault_emer_tx"], sigs,
                                      vault["pubkeys"], self.cosigner_pubkey)
            # And finally amend it and sign it with ALL
            return bump_feerate(self.bitcoind, tx, minimal_feerate - feerate,
                                unvault_amount)

        return form_emer_unvault_tx(vault["unvault_emer_tx"],
                                    vault["unvault_emer_sigs"],
                                    vault["pubkeys"],
                                    self.cosigner_pubkey)

    def add_new_vault(self, output):
        """Add a new vault output to our list.

        :param output: A dict corresponding to an entry of `listunspent`.
        """
        vault = {
            "txid": output["txid"],
            "vout": output["vout"],
            # This amount is in BTC, we want sats
            "amount": int(Decimal(output["amount"]) * Decimal(COIN)),
            # The four pubkeys used in this vault
            "pubkeys": [],
            # For convenience
            "privkey": None,
            "address": output["address"],
            # The *unsigned* first emergency transaction
            "emergency_tx": None,
            # We store the signatures for each transactions as otherwise we
            # would ask all of them to the sig server each time the polling
            # thread is restarted
            "emergency_sigs": [None, None, None, None],
            # More convenient and readable than checking the transaction
            "emergency_signed": False,
            # The unvault transaction, broadcasted to use the spend_tx
            "unvault_tx": None,
            "unvault_sigs": [None, None, None, None],
            "unvault_signed": False,
            # The *unsigned* cancel tx
            "cancel_tx": None,
            "cancel_sigs": [None, None, None, None],
            # The *unsigned* second emergency transaction
            "unvault_emer_tx": None,
            "unvault_emer_sigs": [None, None, None, None],
            # Are cancel and emer signed ? If so we can commit to the unvault.
            "unvault_secure": False,
        }
        index = self.guess_index(vault["address"])
        if index is None:
            raise Exception("No such vault script with our known pubkeys !")
        vault["pubkeys"] = self.get_pubkeys(index)
        vault["privkey"] = self.our_bip32.get_privkey_from_path([index])

        shared_emer_sig = self.create_sign_emergency(vault)
        # Keep it for later
        vault["unvault_sigs"][self.keychains.index(None)] = \
            self.create_sign_unvault(vault)
        # We need to be notified if the vault tx is broadcast, this is the easy
        # way to do so.
        self.watch_unvault(vault)
        cancel_sig = self.create_sign_cancel(vault)
        unvault_emer_sig = self.create_sign_unvault_emer(vault)
        # Send all our sigs but the unvault one, until we are secured
        self.sigserver.send_signature(vault["emergency_tx"].GetTxid().hex(),
                                      shared_emer_sig)
        self.sigserver.send_signature(vault["cancel_tx"].GetTxid().hex(),
                                      cancel_sig)
        self.sigserver.send_signature(vault["unvault_emer_tx"].GetTxid().hex(),
                                      unvault_emer_sig)
        self.vaults.append(vault)

    def poll_for_funds(self):
        """Polls bitcoind to check for received funds.

        If we just went to know of the possession of a new output, it will
        construct the corresponding emergency transaction and spawn a thread
        to fetch emergency transactions signatures.

        Note that this main loop used to be somewhat less naïvely implemented,
        but it was both a premature and useless optimisation.
        """
        while not self.funds_poller_stop.wait(5.0):
            # What we think we have
            known_outputs = [v["txid"] for v in self.vaults]
            # What bitcoind tells we *actually* have
            current_utxos = self.bitcoind.listunspent(
                # Should not be 0 if it was "for real" (cancel_tx's id isn't
                # known for sure)
                minconf=0,
                addresses=self.vault_addresses
            )
            current_utxos_id = [u["txid"] for u in current_utxos]
            spent_vaults = [v for v in self.vaults
                            if v["txid"] not in current_utxos_id]
            new_vaults = [u for u in current_utxos
                          if u["txid"] not in known_outputs]

            for v in spent_vaults:
                # Is it an emergency broadcast ?
                if self.bitcoind.listunspent(
                    minconf=0, addresses=[self.emergency_address]
                ):
                    # Game over.
                    for v in self.vaults:
                        tx = self.get_signed_emergency_tx(v).serialize().hex()
                        if tx is not None:
                            try:
                                self.bitcoind.sendrawtransaction(tx)
                            except bitcoin.rpc.JSONRPCError:
                                # Already sent!
                                pass
                        unvtx = self.get_signed_unvault_emergency_tx(v)
                        hextx = unvtx.serialize().hex()
                        if tx is not None:
                            try:
                                self.bitcoind.sendrawtransaction(hextx)
                            except bitcoin.rpc.JSONRPCError:
                                # Already sent!
                                pass
                    self.stopped = True
                    return

                # If not, it must be an unvault broadcast !
                unvault_addr = CBitcoinAddress.from_scriptPubKey(
                    v["unvault_tx"].vout[0].scriptPubKey
                )
                unvault_utxos = self.bitcoind.listunspent(
                    minconf=0,
                    addresses=[str(unvault_addr)]
                )
                if len(unvault_utxos) == 0:
                    # Maybe someone has already broadcast the cancel
                    # transaction
                    cancel_addr = CBitcoinAddress.from_scriptPubKey(
                        v["cancel_tx"].vout[0].scriptPubKey
                    )
                    assert len(self.bitcoind.listunspent(
                        minconf=0,
                        addresses=[str(cancel_addr)]
                    )) > 0
                else:
                    if v["txid"] not in self.acked_spends:
                        try:
                            tx = self.get_signed_cancel_tx(v).serialize().hex()
                            if tx is not None:
                                self.bitcoind.sendrawtransaction(tx)
                        except bitcoin.rpc.JSONRPCError:
                            # Already sent!
                            pass
                        # FIXME wait for it to be mined ?

            # These were unvaulted
            if len(spent_vaults) > 0:
                self.vaults_lock.acquire()
                self.vaults = [v for v in self.vaults
                               if v not in spent_vaults]
                self.vaults_lock.release()

            for utxo in new_vaults:
                self.vaults_lock.acquire()
                self.add_new_vault(utxo)
                self.vaults_lock.release()
                # Do a new bunch of watchonly imports if we get closer to
                # the maximum index we originally derived.
                # FIXME: This doesn't take address reuse into account
                self.current_index += 1
                self.max_index += 1
                if self.current_index > self.index_treshold - 20:
                    self.update_watched_addresses()

            # If we had new coins, restart the transactions signatures fetcher
            # with the updated list of vaults.
            if len(new_vaults) > 0:
                self.update_sigs_stop.set()
                try:
                    self.update_sigs_thread.join()
                except RuntimeError:
                    # Already dead
                    pass
                self.update_sigs_stop.clear()
                del self.update_sigs_thread
                self.update_sigs_thread = \
                    threading.Thread(target=self.update_all_signatures,
                                     daemon=True)
                self.update_sigs_thread.start()

    def wait_for_unvault_tx(self, vault):
        """Wait until the unvault transaction is signed by everyone."""
        while True:
            self.vaults_lock.acquire()
            signed = vault["unvault_signed"]
            self.vaults_lock.release()
            if signed:
                break
            time.sleep(0.5)

    def create_sign_spend_tx(self, vault, addresses):
        """Create and sign a spend tx which creates a len({address}.keys())
        outputs transaction.

        :return: Our signature for this transaction.
        """
        self.wait_for_unvault_tx(vault)
        unvault_txid = vault["unvault_tx"].GetTxid()
        unvault_value = vault["unvault_tx"].vout[0].nValue
        assert len(vault["unvault_tx"].vout) == 1
        spend_tx = create_spend_tx(unvault_txid, 0, addresses)
        # We use the same pubkeys for the unvault and for the vault
        return sign_spend_tx(spend_tx, vault["privkey"], vault["pubkeys"],
                             self.cosigner_pubkey, unvault_value)

    def initiate_spend(self, vault, addresses):
        """First step to spend, we sign it before handing it to our peer.

        :param vault: The vault to spend, an entry of self.vaults[]
        :param value: How many sats to spend.
        :param addresses: A dictionary containing address as keys and amount to
                          send in sats as value.

        :return: Our signature for this spend transaction.
        """
        return self.create_sign_spend_tx(vault, addresses)

    def accept_spend(self, vault_txid, addresses):
        """We were handed a signature for a spend tx.
        Recreate it, sign it and give our signature to our peer.

        :param vault_txid: The txid of the vault to spend from.
        :param addresses: A dictionary containing address as keys and amount to
                          send in sats as value.

        :return: Our signature for this spend transaction, or None if we don't
                 know about this vault.
        """
        for vault in self.vaults:
            if vault["txid"] == vault_txid:
                return self.create_sign_spend_tx(vault, addresses)
        return None

    def complete_spend(self, vault, peer_pubkey, peer_sig, addresses):
        """Our fellow trader also signed the spend, now ask the cosigner and
        notify other stakeholders we are about to spend a vault. We wait
        synchronously for their response, once again an assumption that's
        a demo!

        :param vault: The vault to spend, an entry of self.vaults[]
        :param peer_pubkey: The other peer's pubkey.
        :param peer_sig: A signature for this spend_tx with the above pubkey.
        :param addresses: A dictionary containing address as keys and amount to
                          send in sats as value.

        :return: A tuple, the fully signed transaction, and whether the spend
                 is accepted
        """
        our_sig = self.create_sign_spend_tx(vault, addresses)
        unvault_txid = vault["unvault_tx"].GetTxid()
        assert len(vault["unvault_tx"].vout) == 1
        unvault_value = vault["unvault_tx"].vout[0].nValue
        cosig = \
            self.cosigner.get_cosignature(unvault_txid[::-1].hex(),
                                          vault["pubkeys"], addresses,
                                          unvault_value)
        spend_tx = create_spend_tx(unvault_txid, 0, addresses)
        # Now the fun part, correctly reconstruct the script
        all_sigs = [bytes(0)] * 3 + [cosig]
        our_pos = vault["pubkeys"].index(CKey(vault["privkey"]).pub)
        peer_pos = vault["pubkeys"].index(peer_pubkey)
        all_sigs[our_pos] = our_sig
        all_sigs[peer_pos] = peer_sig
        spend_tx = form_spend_tx(spend_tx, vault["pubkeys"],
                                 self.cosigner_pubkey, all_sigs)

        # Notify others
        self.sigserver.request_spend(vault["txid"], addresses)
        # Wait for their response, keep it simple..
        while True:
            res = self.sigserver.spend_accepted(vault["txid"])
            if res is not None:
                break
            time.sleep(0.5)

        return spend_tx, res

    def poll_for_spends(self):
        """Poll the sigserver for spend requests.

        Accept the spend if we know the address, refuse otherwise.
        """
        # We do this indefinitely, so we'd better cache the ones we know about
        known_spends = []
        while not self.spends_poller_stop.wait(3.0):
            spends = self.sigserver.get_spends()
            for txid in [txid for txid, addresses in spends.items()
                         if txid not in known_spends]:
                valid_addresses = self.vault_addresses + self.acked_addresses
                # If an output pays to an unknown address refuse.
                if any([address not in valid_addresses
                        for address in spends[txid]]):
                    self.sigserver.refuse_spend(txid, spends[txid])
                # If there is no output to a known address (just change),
                # refuse.
                elif any([address not in self.acked_addresses
                          for address in spends[txid]]):
                    self.sigserver.refuse_spend(txid, spends[txid])
                else:
                    self.sigserver.accept_spend(txid, spends[txid])
                    self.acked_spends.append(txid)
                known_spends.append(txid)

    def update_emergency_signatures(self, vault):
        """Don't stop polling the sig server until we have all the sigs.

        :vault: The dictionary representing the vault we are fetching the
                emergency signatures for.
        """
        txid = vault["emergency_tx"].GetTxid().hex()
        # Poll until finished, or master tells us to stop
        while None in vault["emergency_sigs"]:
            if self.update_sigs_stop.wait(3.0):
                return
            for i in range(1, 5):
                if vault["emergency_sigs"][i - 1] is None:
                    self.vaults_lock.acquire()
                    sig = self.sigserver.get_signature(txid, i)
                    if sig is not None:
                        assert sig[-1] == ALL_ANYONECANPAY
                        vault["emergency_sigs"][i - 1] = sig
                    self.vaults_lock.release()

        # We got all the other signatures, if we append our (SIGHASH_ALL), the
        # transaction MUST be valid.
        emergency_tx = form_emergency_vault_tx(vault["emergency_tx"],
                                               vault["pubkeys"],
                                               vault["emergency_sigs"])
        self.bitcoind.assertmempoolaccept([emergency_tx.serialize().hex()])

        self.vaults_lock.acquire()
        vault["emergency_signed"] = True
        self.vaults_lock.release()

    def update_unvault_emergency(self, vault):
        """Poll the signature server for the unvault_emergency tx signature"""
        txid = vault["unvault_emer_tx"].GetTxid().hex()
        # Poll until finished, or master tells us to stop
        while None in vault["unvault_emer_sigs"]:
            if self.update_sigs_stop.wait(3.0):
                return
            for i in range(1, 5):
                if vault["unvault_emer_sigs"][i - 1] is None:
                    self.vaults_lock.acquire()
                    sig = self.sigserver.get_signature(txid, i)
                    if sig is not None:
                        assert sig[-1] == ALL_ANYONECANPAY
                        vault["unvault_emer_sigs"][i - 1] = sig
                    self.vaults_lock.release()

    def update_cancel_unvault(self, vault):
        """Poll the signature server for the cancel_unvault tx signature"""
        txid = vault["cancel_tx"].GetTxid().hex()
        # Poll until finished, or master tells us to stop
        while None in vault["cancel_sigs"]:
            if self.update_sigs_stop.wait(3.0):
                return
            for i in range(1, 5):
                if vault["cancel_sigs"][i - 1] is None:
                    self.vaults_lock.acquire()
                    sig = self.sigserver.get_signature(txid, i)
                    if sig is not None:
                        assert sig[-1] == ALL_ANYONECANPAY
                        vault["cancel_sigs"][i - 1] = sig
                    self.vaults_lock.release()

    def update_unvault_transaction(self, vault):
        """Get others' sig for the unvault transaction"""
        txid = vault["unvault_tx"].GetTxid().hex()
        # Poll until finished, or master tells us to stop
        while None in vault["unvault_sigs"]:
            if self.update_sigs_stop.wait(3.0):
                return
            for i in range(1, 5):
                if vault["unvault_sigs"][i - 1] is None:
                    self.vaults_lock.acquire()
                    vault["unvault_sigs"][i - 1] = \
                        self.sigserver.get_signature(txid, i)
                    self.vaults_lock.release()

        self.vaults_lock.acquire()
        vault["unvault_signed"] = True
        self.vaults_lock.release()

    def update_unvault_revocations(self, vault):
        """Don't stop polling the sig server until we have all the revocation
        transactions signatures. Then, send our signature for the unvault."""
        self.update_unvault_emergency(vault)
        self.update_cancel_unvault(vault)
        # Ok, all revocations signed we can safely send the unvault sig.
        if None not in vault["unvault_emer_sigs"] + vault["cancel_sigs"]:
            self.vaults_lock.acquire()
            vault["unvault_secure"] = True
            self.vaults_lock.release()
            # We are about to send our commitment to the unvault, be sure to
            # know if funds are spent to it !
            self.sigserver.send_signature(vault["unvault_tx"].GetTxid().hex(),
                                          vault["unvault_sigs"][self.keychains
                                                                .index(None)])
            self.update_unvault_transaction(vault)

    def update_all_signatures(self):
        """Poll the server for the signatures of all transactions."""
        threads = []
        for vault in self.vaults:
            if self.update_sigs_stop.wait(0.0):
                return
            if not vault["emergency_signed"]:
                t = threading.Thread(
                    target=self.update_emergency_signatures, args=[vault]
                )
                t.start()
                threads.append(t)
            if not vault["unvault_secure"]:
                t = threading.Thread(
                    target=self.update_unvault_revocations, args=[vault]
                )
                t.start()
            elif not vault["unvault_signed"]:
                t = threading.Thread(
                    target=self.update_unvault_transaction, args=[vault]
                )
                t.start()

        while len(threads) > 0:
            threads.pop().join()
