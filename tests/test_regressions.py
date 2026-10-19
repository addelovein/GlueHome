"""Offline regressions: synthetic credentials only; no network, keychain, or BLE."""
import asyncio
import base64
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import glue_auth as auth
import glue_ble as ble
import glue_keys as keys
import glue_setup as setup
import glue_storage as storage
import glue_store as store
import glue_sync as sync

LOCK = '00000000-0000-0000-0000-000000000001'


def key(index=1):
    return {'lockId': LOCK, 'keyIndex': index, 'lockOfflineKeyStr': base64.b64encode(b'K' * 112).decode()}


class Isolated(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {'XDG_CONFIG_HOME': self.tmp.name}, clear=True)
        self.env.start(); self.addCleanup(self.env.stop)
        self.values = {'lock-id': LOCK, 'lock-serial': 'TEST-SERIAL'}
        get = patch.object(store, 'get', side_effect=lambda k, default=None: self.values.get(k, default))
        put = patch.object(store, 'set_', side_effect=lambda k,v: self.values.update({k:v}))
        get.start(); put.start(); self.addCleanup(get.stop); self.addCleanup(put.stop)


class BankTests(Isolated):
    def test_reservation_survives_reload_and_refill(self):
        b = keys.KeyBank(LOCK); b.merge([key(1), key(2)])
        self.assertEqual(b.reserve()[0], 1)
        b = keys.KeyBank(LOCK); b.merge([key(1), key(2)])
        self.assertEqual(b.reserve()[0], 2)
        with self.assertRaises(RuntimeError): b.reserve()
        self.assertEqual(b._read()['pending'], [1, 2])

    def test_wrong_lock_and_invalid_keys_rejected(self):
        wrong = key(); wrong['lockId'] = 'different'
        for invalid in ([wrong], [key(), key()], [dict(key(), keyIndex=-1)], [dict(key(), lockOfflineKeyStr='AA==')]):
            with self.assertRaises(ValueError): keys.KeyBank(LOCK).merge(invalid)

    def test_failed_storage_never_issues_key(self):
        b = keys.KeyBank(LOCK); b.merge([key()])
        with patch.object(store, 'set_', side_effect=OSError('disk')):
            with self.assertRaises(OSError): b.reserve()
        self.assertEqual(len(b._read()['keys']), 1)

    def test_sync_retries_failed_burn_before_fetch(self):
        b = keys.KeyBank(LOCK); b.merge([key()]); b.reserve()
        with patch.object(auth, 'report_burn', side_effect=TimeoutError), patch.object(auth, 'api_request') as fetch:
            with self.assertRaises(TimeoutError): b.sync({})
            fetch.assert_not_called()
        with patch.object(auth, 'report_burn') as burn, patch.object(auth, 'api_request', return_value=[key(2)]):
            b.sync({})
            burn.assert_called_once_with({}, LOCK, 1)
        self.assertEqual(b._read()['pending'], [])
        self.assertEqual(b.reserve()[0], 2)

    def test_esphome_ownership_disables_cli_reservations(self):
        b = keys.KeyBank(LOCK); b.merge([key()]); self.values['controller-' + LOCK] = 'esphome'
        with self.assertRaises(RuntimeError): b.reserve()
        self.assertEqual(len(b._read()['keys']), 1)


class AuthTests(Isolated):
    def cfg(self, user='one'):
        return dict(user=user, password='synthetic', pool='pool', client='client', region='region')

    def test_cache_isolation_and_credential_change(self):
        token = 'e30.' + base64.urlsafe_b64encode(json.dumps({'exp': time.time() + 3600}).encode()).decode().rstrip('=') + '.synthetic'
        auth._save_cache(auth._cache_scope(self.cfg()), {'access_token': token})
        self.assertEqual(auth.get_token(self.cfg()), token)
        with patch.object(auth, '_cognito_client', side_effect=RuntimeError('needs auth')):
            with self.assertRaisesRegex(RuntimeError, 'needs auth'): auth.get_token(self.cfg('two'))
            changed = dict(self.cfg(), password='changed')
            with self.assertRaisesRegex(RuntimeError, 'needs auth'): auth.get_token(changed)

    def test_401_reauth_once(self):
        error = urllib.error.HTTPError('https://test', 401, 'Unauthorized', {}, io.BytesIO())
        self.addCleanup(error.close)
        with patch.object(auth, 'get_token', side_effect=['old','new']) as token, patch.object(auth, '_api_get', side_effect=[error, []]) as request:
            self.assertEqual(auth.api_request({}, 'keys/'), [])
            self.assertEqual(request.call_count, 2)
            token.assert_called_with({}, force=True)

    def test_burn_404_requires_pool_confirmation(self):
        error = urllib.error.HTTPError('https://test', 404, 'missing', {}, io.BytesIO())
        self.addCleanup(error.close)
        with patch.object(auth, 'api_request', side_effect=[error, [key()]]):
            with self.assertRaises(RuntimeError): auth.report_burn({}, LOCK, 1)
        with patch.object(auth, 'api_request', side_effect=[error, [key(2)]]):
            auth.report_burn({}, LOCK, 1)

    def test_token_storage_failure_does_not_write_plaintext(self):
        with patch.object(store, 'set_', side_effect=RuntimeError), contextlib.redirect_stderr(io.StringIO()):
            auth._save_cache('test', {'access_token':'synthetic-token'})
        self.assertEqual(list(Path(self.tmp.name).rglob('*.json')), [])


class SessionTests(unittest.IsolatedAsyncioTestCase):
    def session(self, responses):
        class Client:
            async def write_gatt_char(client, _char, data, response=False):
                for frame in responses:
                    session._on_notify(None, frame)
        session = ble.GlueSession(Client())
        return session

    async def test_unrelated_reply_cannot_confirm_operation(self):
        wrong = ble.build_frame(ble.CMD_LOG_IN, b'abc', b'\0')
        s = self.session([wrong])
        with self.assertRaises(asyncio.TimeoutError): await s._send(ble.CMD_OPERATE, b'\1', timeout=0.01)
        self.assertTrue(s._failed)

    async def test_fragmented_and_unrelated_replies(self):
        wrong = ble.build_frame(ble.CMD_LOG_IN, b'xxx', b'\0')
        right = ble.build_frame(ble.CMD_OPERATE, b'abc', b'\0')
        s = self.session([wrong + right[:3], right[3:]])
        self.assertEqual(await s.operate(1), 0)
        self.assertEqual(s.challenge, b'abc')

    async def test_bad_crc_fails_session_closed(self):
        right = ble.build_frame(ble.CMD_OPERATE, b'abc', b'\0')
        bad = bytearray(right); bad[-1] ^= 1
        s = self.session([bad, right])
        with self.assertRaises(asyncio.TimeoutError): await s._send(ble.CMD_OPERATE, timeout=0.01)
        with self.assertRaises(RuntimeError): await s.operate(1)
        self.assertEqual(len(s._buf), 0)

    async def test_bolt_reads_data_not_status(self):
        s = ble.GlueSession(None)
        async def send(cmd, payload=b''):
            return (0, b'\0\xa1')
        s._send = send
        self.assertEqual(await s.bolt_probe(), (0, 0, 0xA1))

    def test_invalid_length_rejected_even_with_valid_crc(self):
        f = bytearray(ble.build_frame(0x32,b'abc',b'\0')); f[1]=255
        f[-2:]=ble.crc16_ccitt(f[:-2]).to_bytes(2,'little')
        self.assertIsNone(ble.parse_frame(f))


class WorkflowTests(Isolated):
    def test_operation_timeout_keeps_prelogin_reservation(self):
        bank = keys.KeyBank(LOCK)
        bank.merge([key()])
        class Session:
            async def login(inner, value):
                self.assertEqual(bank._read()['pending'], [1])
                self.assertEqual(bank._read()['keys'], [])
                return 0
            async def operate(inner, value):
                raise asyncio.TimeoutError()
        async def with_session(fn):
            return await fn(Session())
        with patch.object(ble, '_with_session', side_effect=with_session), patch.object(auth, 'config', return_value={}), patch.object(keys.KeyBank, 'sync', side_effect=TimeoutError), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(asyncio.TimeoutError):
                asyncio.run(ble.cmd_operate('lock'))
        self.assertEqual(bank._read()['pending'], [1])
        with self.assertRaises(RuntimeError): bank.reserve()

    def test_setup_populates_selected_bank_and_preserves_yaml(self):
        root = Path(self.tmp.name); (root/'tools').mkdir(); (root/'esphome').mkdir()
        lock = {'id':LOCK,'serialNumber':'TEST-SERIAL','firmwareVersion':'test'}
        props = [{'name':'Test','doors':[{'name':'Front "door"','locks':[lock]}]}]
        cfg = root/'esphome/glue-test-serial.yaml'; cfg.write_text('hand-edited config')
        with patch.object(setup,'__file__',str(root/'tools/glue_setup.py')), patch('builtins.input',side_effect=['I AGREE','example']), patch.object(setup.getpass,'getpass',return_value='synthetic'), patch.object(auth,'get_token',return_value='token'), patch.object(setup,'_jwt_username',return_value='canonical'), patch.object(auth,'_api_get',side_effect=[props,[key()]]), patch.object(store,'backend_name',return_value='test'), contextlib.redirect_stdout(io.StringIO()):
            setup.main()
        self.assertEqual(cfg.read_text(),'hand-edited config')
        self.assertTrue(cfg.with_name('glue-test-serial.candidate-1.yaml').exists())
        self.assertEqual(keys.identity(),(LOCK,'TEST-SERIAL'))
        self.assertEqual(keys.KeyBank(LOCK).reserve()[0],1)

    def test_yaml_quotes_and_names(self):
        path=Path(self.tmp.name)/'config.yaml'
        setup._emit_yaml(path,{'id':LOCK,'serial':'../Entré / #2','door':'Front "door"\nname','property':'Test'})
        self.assertIn('name: glue-entre-2',path.read_text())
        self.assertIn('Front \\"door\\"\\nname',path.read_text())
        self.assertIn('min_version: 2026.9.1',path.read_text())

    def test_sync_report_before_ack_and_push(self):
        self.values.update({'ha-device':'glue-test','ha-bank-entity':'sensor.glue_bank_status'})
        calls=[]
        def ha(path,data=None):
            calls.append((path,data))
            if path.startswith('states/'):
                return {'state':json.dumps({'lock':LOCK,'pending':[1],'available':[]})}
            return []
        def report(cfg,lock,index):calls.append(('burn',index))
        with patch.object(sync,'ha_request',side_effect=ha), patch.object(auth,'config',return_value={}), patch.object(keys.KeyBank,'sync',return_value=0), patch.object(auth,'report_burn',side_effect=report), patch.object(auth,'api_request',return_value=[key(2)]):
            self.assertTrue(sync.sync_once())
        self.assertEqual(calls[1][0],'burn')
        self.assertTrue(calls[2][0].endswith('_ack_burn'))
        self.assertTrue(calls[3][0].endswith('_add_key'))
        self.assertEqual(self.values['controller-'+LOCK],'esphome')

    def test_sync_does_not_ack_failed_burn(self):
        self.values.update({'ha-device':'glue-test','ha-bank-entity':'sensor.bank'})
        with patch.object(sync,'ha_request',return_value={'state':json.dumps({'lock':LOCK,'pending':[1],'available':[]})}) as ha, patch.object(auth,'config',return_value={}), patch.object(keys.KeyBank,'sync',return_value=0), patch.object(auth,'report_burn',side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):sync.sync_once()
            self.assertEqual(ha.call_count,1)

    def test_return_to_ble_requires_confirmed_device_retirement(self):
        self.values.update({'ha-device':'glue-test','ha-bank-entity':'sensor.bank','controller-'+LOCK:'esphome'})
        keys.KeyBank(LOCK).merge([key(1), key(2)])
        status = {'lock':LOCK,'pending':[],'available':[1,2],'paused':False,'floor':-1}
        with patch.object(sync,'ha_request',return_value={'state':json.dumps(status)}) as ha:
            self.assertFalse(sync.sync_once('ble'))
            self.assertTrue(ha.call_args[0][0].endswith('_retire_keys'))
            self.assertEqual(self.values['controller-'+LOCK],'esphome')
        status.update(paused=True,pending=[],available=[],floor=2)
        with patch.object(sync,'ha_request',return_value={'state':json.dumps(status)}), patch.object(auth,'config',return_value={}), patch.object(auth,'api_request',return_value=[key(2),key(3)]):
            self.assertTrue(sync.sync_once('ble'))
        self.assertEqual(keys.KeyBank(LOCK).reserve()[0],3)
        with patch.object(sync,'ha_request',return_value={'state':json.dumps(status)}):
            with self.assertRaisesRegex(RuntimeError,'explicitly'):sync.sync_once()


class EncryptedStorageTests(unittest.TestCase):
    def setUp(self):
        try:
            import cryptography
        except ImportError:
            self.skipTest('cryptography not installed in this interpreter')
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.env=patch.dict(os.environ,{'XDG_CONFIG_HOME':self.tmp.name,'GLUE_STORAGE_KEY':base64.b64encode(b'X'*32).decode()},clear=True)
        self.env.start();self.addCleanup(self.env.stop)

    def test_encrypted_roundtrip_permissions_and_tamper(self):
        store.set_('password','synthetic-sensitive-value')
        path=storage.config_dir()/'secrets.enc'
        self.assertNotIn(b'synthetic-sensitive-value',path.read_bytes())
        self.assertEqual(store.get('password'),'synthetic-sensitive-value')
        if os.name!='nt':self.assertEqual(path.stat().st_mode&0o777,0o600)
        data=bytearray(path.read_bytes());data[-1]^=1;path.write_bytes(data)
        with self.assertRaises(Exception):store.get('password')
        with self.assertRaises(Exception):store.set_('username','another')

    def test_environment_wins(self):
        store.set_('password','saved')
        os.environ['GLUE_PASSWORD']='override'
        self.assertEqual(store.get('password'),'override')

    def test_updates_repair_mode_and_internal_state_ignores_env(self):
        store.set_('bank-test', 'new-state')
        path = storage.config_dir() / 'secrets.enc'
        path.chmod(0o644)
        os.environ['GLUE_BANK_TEST'] = 'stale-state'
        store.set_('bank-test', 'reserved-state')
        self.assertEqual(store.get('bank-test'), 'reserved-state')
        if os.name != 'nt': self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_cross_process_reservation_is_unique(self):
        bank=keys.KeyBank(LOCK);bank.merge([key(1),key(2)])
        script='import sys;sys.path.insert(0,"tools");from glue_keys import KeyBank;print(KeyBank("'+LOCK+'").reserve()[0])'
        procs=[subprocess.Popen([sys.executable,'-c',script],stdout=subprocess.PIPE,stderr=subprocess.PIPE) for _ in range(2)]
        result=[]
        for proc in procs:
            out,err=proc.communicate(timeout=20)
            self.assertEqual(proc.returncode,0,err.decode());result.append(int(out))
        self.assertEqual(sorted(result),[1,2])


if __name__=='__main__':unittest.main()
