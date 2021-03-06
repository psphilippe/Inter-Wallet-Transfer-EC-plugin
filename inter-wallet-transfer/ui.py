from PyQt5.QtGui import *
from PyQt5.QtCore import *
from PyQt5.QtWidgets import *

from electroncash.i18n import _
from electroncash_gui.qt.util import MessageBoxMixin, MyTreeWidget
from electroncash import keystore
from electroncash.wallet import Standard_Wallet
from electroncash.storage import WalletStorage
from electroncash.keystore import Hardware_KeyStore
from electroncash_gui.qt.util import *
from electroncash.transaction import Transaction, TYPE_ADDRESS
from electroncash.util import PrintError, print_error, age, Weak, InvalidPassword
import time, datetime, random, threading, tempfile, string, os, queue
from enum import IntEnum

def get_name(utxo) -> str:
    return "{}:{}".format(utxo['prevout_hash'], utxo['prevout_n'])

class LoadRWallet(MessageBoxMixin, PrintError, QWidget):

    def __init__(self, parent, plugin, wallet_name, recipient_wallet=None, time=None, password=None):
        QWidget.__init__(self, parent)
        self.password = password
        self.wallet = parent.wallet
        self.utxos = self.wallet.get_spendable_coins(None, parent.config)
        random.shuffle(self.utxos)  # randomize the coins' order
        for x in range(10):
            name = 'tmp_wo_wallet' + ''.join(random.choices(string.ascii_letters + string.digits, k=10))
            self.file = os.path.join(tempfile.gettempdir(), name)
            if not os.path.exists(self.file):
                break
        else:
            raise RuntimeError('Could not find a unique temp file in tmp directory', tempfile.gettempdir())
        self.tmp_pass = ''.join(random.choices(string.ascii_uppercase + string.digits, k=10))
        self.storage=None
        self.recipient_wallet=None
        self.keystore=None
        self.plugin = plugin
        self.network = parent.network
        self.wallet_name = wallet_name
        self.keystore = None
        vbox = QVBoxLayout()
        self.setLayout(vbox)
        self.local_xpub = self.wallet.get_master_public_keys()
        l = QLabel(_("Master Public Key") + _(" of this wallet (used to generate all of your addresses): "))
        l2 = QLabel((self.local_xpub and self.local_xpub[0]) or _("This wallet is <b>non-deterministic</b> and cannot be used as a transfer destination."))
        vbox.addWidget(l)
        vbox.addWidget(l2)
        l2.setTextInteractionFlags(Qt.TextSelectableByMouse)
        l = QLabel(_("Master Public Key") + " of the wallet you want to transfer your funds to:")
        disabled = False
        if self.wallet.is_watching_only():
            l.setText(_("This wallet is <b>watching-only</b> and cannot be used as a transfer source."))
            disabled = True
        elif any([isinstance(k, Hardware_KeyStore) for k in self.wallet.get_keystores()]):
            l.setText(_("This wallet is a <b>hardware wallet</b> and cannot be used as a transfer source."))
            disabled = True
        vbox.addWidget(l)
        self.xpubkey=None
        self.xpubkey_wid = QLineEdit()
        self.xpubkey_wid.textEdited.connect(self.transfer_changed)
        self.xpubkey_wid.setDisabled(disabled)
        vbox.addWidget(self.xpubkey_wid)
        l = QLabel(_("How long the transfer should take (in whole hours): "))
        vbox.addWidget(l)
        l.setDisabled(disabled)
        self.time_e = QLineEdit()
        self.time_e.setMaximumWidth(70)
        self.time_e.textEdited.connect(self.transfer_changed)
        self.time_e.setDisabled(disabled)
        hbox = QHBoxLayout()
        vbox.addLayout(hbox)
        hbox.addWidget(self.time_e)
        self.speed = QLabel()
        hbox.addWidget(self.speed)
        hbox.addStretch(1)
        self.transfer_button = QPushButton(_("Transfer"))
        self.transfer_button.clicked.connect(self.transfer)
        vbox.addWidget(self.transfer_button)
        self.transfer_button.setDisabled(True)

        vbox.addStretch(1)

    def filter(self, *args):
        ''' This is here because searchable_list must define a filter method '''

    def showEvent(self, e):
        super().showEvent(e)
        if not self.network and self.isEnabled():
            self.show_warning(_("The Inter-Wallet Transfer plugin cannot function in offline mode. Restart Electron Cash in online mode to proceed."))
            self.setDisabled(True)


    @staticmethod
    def delete_temp_wallet_file(file):
        ''' deletes the wallet file '''
        if file and os.path.exists(file):
            try:
                os.remove(file)
                print_error("[InterWalletTransfer] Removed temp file", file)
            except Exception as e:
                print_error("[InterWalletTransfer] Failed to remove temp file", file, "error: ", repr(e))

    def transfer(self):
        self.show_message(_("You should not use either wallet during the transfer. Leave Electron Cash active. "
                            "The plugin ceases operation and will have to be re-activated if Electron Cash "
                            "is stopped during the operation."))
        self.storage = WalletStorage(self.file)
        self.storage.set_password(self.tmp_pass, encrypt=True)
        self.storage.put('keystore', self.keystore.dump())
        self.recipient_wallet = Standard_Wallet(self.storage)
        self.recipient_wallet.start_threads(self.network)
        # comment the below out if you want to disable auto-clean of temp file
        # otherwise the temp file will be auto-cleaned on app exit or
        # on the recepient_wallet object's destruction (when refct drops to 0)
        Weak.finalize(self.recipient_wallet, self.delete_temp_wallet_file, self.file)
        self.plugin.switch_to(Transfer, self.wallet_name, self.recipient_wallet, float(self.time_e.text()), self.password)

    def transfer_changed(self):
        try:
            assert float(self.time_e.text()) > 0
            self.xpubkey = self.xpubkey_wid.text()
            self.keystore = keystore.from_master_key(self.xpubkey)
        except:
            self.speed.setText('')
            self.transfer_button.setDisabled(True)
        else:
            self.transfer_button.setDisabled(False)
            v = len(self.utxos) / float(self.time_e.text())
            self.speed.setText('{0:.2f}'.format(v)+' tx/h on average')


class TransferringUTXO(MessageBoxMixin, PrintError, MyTreeWidget):

    update_sig = pyqtSignal()

    class DataRoles(IntEnum):
        Time = Qt.UserRole+1
        Name = Qt.UserRole+2

    def __init__(self, parent, tab):
        MyTreeWidget.__init__(self, parent, self.create_menu,[
            _('Address'),
            _('Amount'),
            _('Time'),
            _('When'),
            _('Status'),
        ], stretch_column=3, deferred_updates=True)
        self.tab = Weak.ref(tab)
        self.t0 = time.time()
        self.t0_last = None
        self._recalc_times(tab.times)
        self.print_error("transferring utxo")
        self.utxos = list(tab.utxos)
        self.main_window = parent
        self.setSelectionMode(QAbstractItemView.NoSelection)
        self.setSortingEnabled(False)
        self.sent_utxos = dict()
        self.failed_utxos = dict()
        self.sending = None
        self.check_icon = self._get_check_icon()
        self.fail_icon = self._get_fail_icon()
        self.update_sig.connect(self.update)
        self.monospace_font = QFont(MONOSPACE_FONT)
        self.italic_font = QFont(); self.italic_font.setItalic(True)
        self.timer = QTimer(self)
        self.timer.setSingleShot(False)
        self.timer.timeout.connect(self.update_sig)
        self.timer.start(2000)  # update every 2 seconds since the granularity of our "When" column is ~5 seconds
        self.wallet = tab.recipient_wallet

    def create_menu(self, position):
        pass

    @staticmethod
    def _get_check_icon() -> QIcon:
        if QFile.exists(":icons/confirmed.png"):
            # old EC version
            return QIcon(":icons/confirmed.png")
        else:
            # newer EC version
            return QIcon(":icons/confirmed.svg")

    @staticmethod
    def _get_fail_icon() -> QIcon:
        if QFile.exists(":icons/warning.png"):
            # current EC version
            return QIcon(":icons/warning.png")
        else:
            # future EC version
            return QIcon(":icons/warning.svg")

    def _recalc_times(self, times):
        if self.t0_last != self.t0:
            now = self.t0  # t0 is updated by thread as the actual start time
            self.times = [ time.localtime(now + s) for s in times ]
            self.times_secs = times
            self.t0_last = now

    def on_update(self):
        self.clear()
        tab = self.tab()
        if not tab or not self.wallet:
            return
        self._recalc_times(tab.times)
        base_unit = self.main_window.base_unit()
        for i, u in enumerate(self.utxos):
            address = u['address'].to_ui_string()
            value = self.main_window.format_amount(u['value'], whitespaces=True) + " " + base_unit
            name = get_name(u)
            ts = self.sent_utxos.get(name)
            icon = None
            when_font = None
            when = ''
            is_sent = ts is not None
            if is_sent:
                status = _("Sent")
                when = age(ts, include_seconds=True)
                icon = self.check_icon
            else:
                failed_reason = self.failed_utxos.get(name)
                if failed_reason:
                    status = _("Failed")
                    when = failed_reason
                    icon = self.fail_icon
                    when_font = self.italic_font
                elif name == self.sending:
                    status = _("Processing")
                    when = status + " ..."
                    when_font = self.italic_font
                else:
                    status = _("Queued")
                    when = age(max(self.t0 + self.times_secs[i], time.time()+0.5), include_seconds=True)

            item = SortableTreeWidgetItem([address, value, time.strftime('%H:%M', self.times[i]), when, status])
            item.setFont(0, self.monospace_font)
            item.setFont(1, self.monospace_font)
            item.setTextAlignment(1, Qt.AlignLeft)
            if icon:
                item.setIcon(4, icon)
            if when_font:
                item.setFont(3, when_font)
            self.addChild(item)


class Transfer(MessageBoxMixin, PrintError, QWidget):

    switch_signal = pyqtSignal()
    done_signal = pyqtSignal(str)
    set_label_signal = pyqtSignal(str, str)

    def __init__(self, parent, plugin, wallet_name, recipient_wallet, hours, password):
        QWidget.__init__(self, parent)
        self.wallet_name = wallet_name
        self.plugin = plugin
        self.password = password
        self.main_window = parent
        self.wallet = parent.wallet
        self.recipient_wallet = recipient_wallet

        cancel = False

        self.utxos = self.wallet.get_spendable_coins(None, parent.config)
        if not self.utxos:
            self.main_window.show_message(_("No coins were found in this wallet; cannot proceed with transfer."))
            cancel = True
        elif self.wallet.has_password():
            self.main_window.show_error(_(
                "Inter-Wallet Transfer plugin requires the password. "
                "It will be sending transactions from this wallet at a random time without asking for confirmation."))
            while True:
                # keep trying the password until it's valid or user cancels
                self.password = self.main_window.password_dialog()
                if not self.password:
                    # user cancel
                    cancel = True
                    break
                try:
                    self.wallet.check_password(self.password)
                    break  # password was good, break out of loop
                except InvalidPassword as e:
                    self.show_warning(str(e))  # show error, keep looping

        random.shuffle(self.utxos)
        self.times = self.randomize_times(hours)
        self.tu = TransferringUTXO(parent, self)
        vbox = QVBoxLayout()
        self.setLayout(vbox)
        vbox.addWidget(self.tu)
        self.tu.update()
        self.abort_but = b = QPushButton(_("Abort"))
        b.clicked.connect(self.abort)
        vbox.addWidget(b)
        self.switch_signal.connect(self.switch_signal_slot)
        self.done_signal.connect(self.done_slot)
        self.set_label_signal.connect(self.set_label_slot)
        self.sleeper = queue.Queue()
        if not cancel:
            self.t = threading.Thread(target=self.send_all, daemon=True)
            self.t.start()
        else:
            self.t = None
            self.setDisabled(True)
            # fire the switch signal as soon as we return to the event loop
            QTimer.singleShot(0, self.switch_signal)

    def filter(self, *args):
        ''' This is here because searchable_list must define a filter method '''

    def diagnostic_name(self):
        return "InterWalletTransfer.Transfer"

    def randomize_times(self, hours):
        times = [random.randint(0,int(hours*3600)) for t in range(len(self.utxos))]
        times.insert(0, 0)  # first time is always immediate
        times.sort()
        del times[-1]  # since we inserted 0 at the beginning
        assert len(times) == len(self.utxos)
        return times

    def send_all(self):
        ''' Runs in a thread '''
        def wait(timeout=1.0) -> bool:
            try:
                self.sleeper.get(timeout=timeout)
                # if we get here, we were notified to abort.
                return False
            except queue.Empty:
                '''Normal course of events, we slept for timeout seconds'''
                return True
        self.tu.t0 = t0 = time.time()
        ct, err_ct = 0, 0
        for i, t in enumerate(self.times):
            def time_left():
                return (t0 + t) - time.time()
            while time_left() > 0.0:
                if not wait(max(0.0, time_left())):  # wait for "time left" seconds
                    # abort signalled
                    return
            coin = self.utxos.pop(0)
            name = get_name(coin)
            self.tu.sending = name
            self.tu.update_sig.emit()  # have the widget immediately display "Processing"
            while not self.recipient_wallet.is_up_to_date():
                ''' We must wait for the recipient wallet to finish synching...
                Ugly hack.. :/ '''
                self.print_error("Receiving wallet is not yet up-to-date... waiting... ")
                if not wait(5.0):
                    # abort signalled
                    return
            err = self.send_tx(coin)
            if not err:
                self.tu.sent_utxos[name] = time.time()
                ct += 1
            else:
                self.tu.failed_utxos[name] = err
                err_ct += 1
            self.tu.sending = None
            self.tu.update_sig.emit()  # have the widget immediately show "Sent or "Failed"
        # Emit a signal which will end up calling switch_signal_slot
        # in the main thread; we need to do this because we must now update
        # the GUI, and we cannot update the GUI in non-main-thread
        # See issue #10
        if err_ct:
            self.done_signal.emit(_("Transferred {num} coins successfully, {failures} coins failed").format(num=ct, failures=err_ct))
        else:
            self.done_signal.emit(_("Transferred {num} coins successfully").format(num=ct))

    def clean_up(self):
        if self.recipient_wallet:
            self.recipient_wallet.stop_threads()
        self.recipient_wallet = None
        if self.tu:
            self.tu.wallet = None
            if self.tu.timer:
                self.tu.timer.stop()
                self.tu.timer.deleteLater()
                self.tu.timer = None

    def switch_signal_slot(self):
        ''' Runs in GUI (main) thread '''
        self.clean_up()
        self.plugin.switch_to(LoadRWallet, self.wallet_name, None, None, None)

    def done_slot(self, msg):
        self.abort_but.setText(_("Back"))
        self.show_message(msg)

    def send_tx(self, coin: dict) -> str:
        ''' Returns the failure reason as a string on failure, or 'None'
        on success. '''
        self.wallet.add_input_info(coin)
        inputs = [coin]
        recipient_address = self.recipient_wallet and self.recipient_wallet.get_unused_address()
        if not recipient_address:
            self.print_error("Could not get recipient_address; recipient wallet may have been cleaned up, aborting send_tx")
            return _("Unspecified failure")
        outputs = [(recipient_address.kind, recipient_address, coin['value'])]
        kwargs = {}
        if hasattr(self.wallet, 'is_schnorr_enabled'):
            # This EC version has Schnorr, query the flag
            kwargs['sign_schnorr'] = self.wallet.is_schnorr_enabled()
        # create the tx once to get a fee from the size
        tx = Transaction.from_io(inputs, outputs, locktime=self.wallet.get_local_height(), **kwargs)
        fee = tx.estimated_size()
        if coin['value'] - fee < self.wallet.dust_threshold():
            self.print_error("Resulting output value is below dust threshold, aborting send_tx")
            return _("Too small")
        # create the tx again, this time with the real fee
        outputs = [(recipient_address.kind, recipient_address, coin['value'] - fee)]
        tx = Transaction.from_io(inputs, outputs, locktime=self.wallet.get_local_height(), **kwargs)
        try:
            self.wallet.sign_transaction(tx, self.password)
        except InvalidPassword as e:
            return str(e)
        except Exception as e:
            return _("Unspecified failure")

        self.set_label_signal.emit(tx.txid(),
            _("Inter-Wallet Transfer {amount} -> {address}").format(
                amount = self.main_window.format_amount(coin['value']) + " " + self.main_window.base_unit(),
                address = recipient_address.to_ui_string()
        ))
        try:
            self.main_window.network.broadcast_transaction2(tx)
        except Exception as e:
            self.print_error("Error broadcasting tx:", repr(e))
            return (e.args and e.args[0]) or _("Unspecified failure")
        return None

    def set_label_slot(self, txid: str, label: str):
        ''' Runs in GUI (main) thread '''
        self.wallet.set_label(txid, label)

    def abort(self):
        self.kill_join()
        self.switch_signal.emit()

    def kill_join(self):
        if self.t and self.t.is_alive():
            self.sleeper.put(None)  # notify thread to wake up and exit
            if threading.current_thread() is not self.t:
                self.t.join(timeout=2.5)  # wait around a bit for it to die but give up if this takes too long

    def on_delete(self):
        pass

    def on_update(self):
        pass
