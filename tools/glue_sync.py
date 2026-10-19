"""Reconcile one ESPHome bank through Home Assistant's authenticated REST API.

Run once or with --watch; credentials come only from the shared secure store/env.
GLUE_HA_URL (HTTPS), GLUE_HA_TOKEN, GLUE_HA_DEVICE (ESPHome node name),
GLUE_HA_BANK_ENTITY (sensor entity ID). This worker never operates the motor.
"""
import argparse
import ipaddress
import json
import os
import re
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlsplit

import glue_auth
import glue_store
from glue_keys import identity, KeyBank, validate_keys
from glue_storage import locked


def _required(name):
    value = glue_store.get(name)
    if not value:
        raise RuntimeError('Missing ' + glue_store._env_name(name))
    return value


_INTERNAL_HOSTNAMES = ('localhost', 'homeassistant', 'supervisor', 'hassio')


def _is_local_host(hostname):
    """True if plain HTTP to this host stays on the local network: an internal
    add-on hostname, or a private / loopback / link-local IP address."""
    if not hostname:
        return False
    if hostname in _INTERNAL_HOSTNAMES:
        return True
    try:
        ip = ipaddress.ip_address(hostname.strip('[]'))
    except ValueError:
        return False  # a bare public hostname must use HTTPS
    return ip.is_private or ip.is_loopback or ip.is_link_local


def _ha_endpoint():
    """Return (base_api_url, bearer_token). Inside an HA add-on, default to the
    Supervisor's Core API proxy (http://supervisor/core/api) with SUPERVISOR_TOKEN
    — no manual URL/token needed and it always routes correctly. An explicit
    GLUE_HA_URL (+ GLUE_HA_TOKEN) overrides this for non-add-on use."""
    url = glue_store.get('ha-url')
    token = glue_store.get('ha-token')
    supervisor = os.environ.get('SUPERVISOR_TOKEN')
    if not url and supervisor:
        return 'http://supervisor/core/api', supervisor
    if not url:
        raise RuntimeError('Set GLUE_HA_URL + GLUE_HA_TOKEN, or run inside an HA add-on (SUPERVISOR_TOKEN)')
    base = url.rstrip('/')
    parsed = urlsplit(base)
    # Plain HTTP is permitted only to a host that stays on the local network:
    # loopback, HA's internal add-on hostnames, or a private/link-local IP
    # (RFC 1918 / RFC 4193 / loopback). A public host must use HTTPS so the
    # bearer token is never sent in clear text over the open internet.
    if parsed.scheme != 'https' and not (parsed.scheme == 'http' and _is_local_host(parsed.hostname)):
        raise ValueError('Use HTTPS for a public HA, or HTTP only on a local/private address')
    if not token:
        raise RuntimeError('GLUE_HA_TOKEN is required with GLUE_HA_URL')
    return base + '/api', token


def ha_request(path, data=None):
    api_base, token = _ha_endpoint()
    url = api_base + '/' + path
    req = urllib.request.Request(url,
        data=None if data is None else json.dumps(data).encode(),
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    try:
        with urllib.request.build_opener(glue_auth._NoRedirect).open(req, timeout=15) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        # Make misconfiguration diagnosable: a 404 is usually a wrong entity id,
        # a 401 a bad/expired token. Print the request so the cause is obvious.
        print(f'HA API {exc.code} for {url} (check ha_token and ha_bank_entity)', flush=True)
        raise


def sync_once(controller=None):
    lock_id, _ = identity()
    device = _required('ha-device').replace('-', '_')
    entity = _required('ha-bank-entity')
    if not re.fullmatch(r'[a-z0-9_]+', device) or not re.fullmatch(r'sensor\.[a-z0-9_]+', entity):
        raise ValueError('Invalid HA device/service or sensor entity name')
    with locked('sync-' + lock_id):
        state = ha_request('states/' + entity)['state']
        if state in ('busy', 'unknown', 'unavailable'):
            return False
        bank = json.loads(state)
        if bank.get('lock') != lock_id:
            raise RuntimeError('ESPHome bank belongs to another lock')
        for field in ('pending', 'available'):
            if not isinstance(bank.get(field), list) or len(bank[field]) > 8 or any(type(i) is not int or not 0 <= i <= 2147483647 for i in bank[field]):
                raise RuntimeError('Invalid ESPHome bank status')
        floor = bank.get('floor', -1)
        if type(floor) is not int or not -1 <= floor <= 2147483647:
            raise RuntimeError('Invalid device retirement watermark')
        if type(bank.get('paused', False)) is not bool or (bank.get('paused', False) and bank['available']):
            raise RuntimeError('Inconsistent device pause state')
        service = 'services/esphome/' + device
        if controller == 'ble':
            if not bank.get('paused', False):
                ha_request(service + '_retire_keys', {})
                return False  # wait for sensor confirmation, not just HTTP dispatch
            cfg = glue_auth.config()
            for index in bank['pending']:
                glue_auth.report_burn(cfg, lock_id, index)
                ha_request(service + '_ack_burn', {'index': index})
            if bank['pending']:
                return False
            with locked('ble-' + lock_id):
                local = KeyBank(lock_id)
                local.retire_through(floor)
                local.sync(cfg)
                with locked(local.name):
                    glue_store.set_('controller-' + lock_id, 'ble')
            return True
        if glue_store.get('controller-' + lock_id) == 'ble' and controller != 'esphome':
            raise RuntimeError('Bank belongs to BLE; explicitly request --controller esphome to transfer it')
        if bank.get('paused', False):
            if controller != 'esphome':
                raise RuntimeError('Device paused; explicitly request --controller esphome')
            cfg = glue_auth.config()
            for index in bank['pending']:
                glue_auth.report_burn(cfg, lock_id, index)
                ha_request(service + '_ack_burn', {'index': index})
            if not bank['pending']:
                ha_request(service + '_resume_keys', {})
            return False
        # Nothing to do: ESPHome already owns a full bank with no burns to report.
        # Skip all Glue cloud calls so an idle lock polls only local HA, never the
        # (abandoned) Glue server. We act on Glue only after an actual unlock.
        MAX_BANK = 5
        if (glue_store.get('controller-' + lock_id) == 'esphome'
                and not bank['pending'] and len(bank['available']) >= MAX_BANK):
            return True
        # Handoff waits for any CLI session to finish. Persist ownership BEFORE
        # handing keys to ESPHome; Python BLE cannot subsequently reuse them.
        with locked('ble-' + lock_id):
            cfg = glue_auth.config()
            local = KeyBank(lock_id)
            local.sync(cfg)
            with locked(local.name):
                glue_store.set_('controller-' + lock_id, 'esphome')
        for index in bank['pending']:
            glue_auth.report_burn(cfg, lock_id, index)
            ha_request(service + '_ack_burn', {'index': index})
        keys = validate_keys(lock_id, glue_auth.api_request(cfg, 'glue-osm-devices/' + quote(lock_id, safe='') + '/keys/'))
        pushed = False
        for key in keys:
            if key['keyIndex'] in bank['pending'] or key['keyIndex'] in bank['available']:
                continue
            ha_request(service + '_add_key', {'index': key['keyIndex'], 'key': key['lockOfflineKeyStr']})
            pushed = True
        # HA returning HTTP 200 only confirms dispatch. Subsequent status polls
        # are authoritative; busy/rejected/lost calls are retried next cycle.
        return not pushed and not bank['pending'] and bool(bank['available']) if controller == 'esphome' else True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--watch', action='store_true', help='reconcile every 15 seconds; retry outages')
    parser.add_argument('--controller', choices=['ble', 'esphome'], help='explicitly transfer ownership; retires old keys first')
    args = parser.parse_args()
    while True:
        try:
            complete = sync_once(args.controller)
            print('Reconciled' if complete else 'Device busy/offline or transfer pending')
            if complete and args.controller:
                return
        except Exception as exc:
            if not args.watch:
                raise
            print('Sync pending (' + type(exc).__name__ + '); will retry', flush=True)
        if not args.watch:
            return
        time.sleep(15)


if __name__ == '__main__':
    main()
