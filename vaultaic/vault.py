import bitcoin.rpc
import requests

from bip32 import BIP32
from bitcoin.wallet import CBitcoinAddress
from .transactions import (
    vault_txout,
)


class Vault:
    """The vault from the viewpoint of one of the stakeholders.

    Allows to derive the next key of the HD wallets of all the stakeholders, to
    deterministically derive each vault.
    Builds and signs all the necessary transactions when spending from the
    vault.
    """
    def __init__(self, xpriv, xpubs, server_pubkey, emergency_pubkeys,
                 bitcoin_conf_path, sigserver_url, current_index=0):
        """
        We need the xpub of all the other stakeholders to derive their pubkeys.

        :param xpriv: Who am I ? Has to correspond to one of the following
                      xpub. As str.
        :param xpubs: A list of the xpub of all the stakeholders (as str), in
                      the following order: 1) first trader 2) second trader
                      3) first "normie" stakeholder 4) second "normie"
                      stakeholder.
        :param server_pubkey: The public key of the co-signing server.
        :param emergency_pubkeys: A list of the four offline keys of the
                                  stakeholders, as bytes.
        :param bitcoin_conf_path: Path to bitcoin.conf.
        :param sigserver_url: The url of the server to post / get the sigs from
                              other stakeholders.
        """
        assert len(xpubs) == 4
        self.our_bip32 = BIP32.from_xpriv(xpriv)
        self.keychains = []
        for xpub in xpubs:
            if xpub != self.our_bip32.get_master_xpub():
                self.keychains.append(BIP32.from_xpub(xpub))
            else:
                self.keychains.append(None)
        self.server_pubkey = server_pubkey
        self.emergency_pubkeys = emergency_pubkeys
        self.bitcoind = bitcoin.rpc.RawProxy(btc_conf_file=bitcoin_conf_path)
        self.sigserver_url = sigserver_url
        if self.sigserver_url.endswith('/'):
            self.sigserver_url = self.sigserver_url[:-1]
        self.current_index = current_index
        self.update_watched_addresses()

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

    def watch_output(self, txo):
        """Import this output as watchonly to bitcoind.

        :param txo: The output to watch, a CTxOutput.
        """
        addr = str(CBitcoinAddress.from_scriptPubKey(txo.scriptPubKey))
        self.bitcoind.importaddress(addr, "vaultaic", True)

    def update_watched_addresses(self):
        """Update the watchonly addresses"""
        # FIXME: We need something more robust than assuming other stakeholders
        # won't be more out of sync than +100 of the index.
        for i in range(self.current_index, self.current_index + 100):
            pubkeys = self.get_pubkeys(i)
            self.watch_output(vault_txout(pubkeys, 0))

    def getnewaddress(self):
        """Get the next vault address, we bump the derivation index.

        :return: (str) The next vault address.
        """
        pubkeys = self.get_pubkeys(self.current_index)
        txo = vault_txout(pubkeys, 0)
        addr = str(CBitcoinAddress.from_scriptPubKey(txo.scriptPubKey))
        # Bump afterwards..
        self.current_index += 1
        self.update_watched_addresses()
        return addr

    def send_signature(self, txid, sig):
        """Send the signature {sig} for tx {txid} to the sig server."""
        if isinstance(sig, bytes):
            sig = sig.hex()
        elif not isinstance(sig, str):
            raise Exception("The signature must be either bytes or a valid hex"
                            " string")
        stakeholder_id = self.keychains.index(None) + 1
        r = requests.post("{}/sig/{}/{}".format(self.sigserver_url, txid,
                                                stakeholder_id),
                          data={"sig": sig})
        if not r.status_code == 201:
            raise Exception("stakeholder #{}: Could not send sig '{}' for"
                            " txid {}.".format(stakeholder_id, sig, txid))