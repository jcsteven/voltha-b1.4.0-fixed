# Copyright 2017-present Adtran, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Adtran generic VOLTHA device handler
"""
import argparse
import datetime
import shlex
import time

import arrow
import structlog
from twisted.internet import reactor, defer
from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.python.failure import Failure

from voltha.adapters.adtran_olt.net.adtran_netconf import AdtranNetconfClient
from voltha.adapters.adtran_olt.net.adtran_rest import AdtranRestClient
from voltha.protos import third_party
from voltha.protos.common_pb2 import OperStatus, AdminState, ConnectStatus
from voltha.protos.logical_device_pb2 import LogicalDevice
from voltha.protos.openflow_13_pb2 import ofp_desc, ofp_switch_features, OFPC_PORT_STATS, \
    OFPC_GROUP_STATS, OFPC_TABLE_STATS, OFPC_FLOW_STATS
from alarms.adapter_alarms import AdapterAlarms
from pki.olt_pm_metrics import OltPmMetrics
from common.utils.asleep import asleep

_ = third_party

DEFAULT_MULTICAST_VLAN = 4000
DEFAULT_UTILITY_VLAN = 4094
DEFAULT_UNTAGGED_VLAN = DEFAULT_UTILITY_VLAN    # if RG does not send priority tagged frames
#DEFAULT_UNTAGGED_VLAN = 4092

_DEFAULT_RESTCONF_USERNAME = ""
_DEFAULT_RESTCONF_PASSWORD = ""
_DEFAULT_RESTCONF_PORT = 8081

_DEFAULT_NETCONF_USERNAME = ""
_DEFAULT_NETCONF_PASSWORD = ""
_DEFAULT_NETCONF_PORT = 830


class AdtranDeviceHandler(object):
    """
    A device that supports the ADTRAN RESTCONF protocol for communications
    with a VOLTHA/VANILLA managed device.
    Port numbering guidelines for Adtran OLT devices.  Derived classes may augment
    the numbering scheme below as needed.

      - Reserve port 0 for the CPU capture port. All ports to/from this port should
        be related to messages destined to/from the OpenFlow controller.

      - Begin numbering northbound ports (network facing) at port 1 contiguously.
        Consider the northbound ports to typically be the highest speed uplinks.
        If these ports are removable or provided by one or more slots in a chassis
        subsystem, still reserve the appropriate amount of port numbers whether they
        are populated or not.

      - Number southbound ports (customer facing) ports next starting at the next
        available port number. If chassis based, follow the same rules as northbound
        ports and reserve enough port numbers.

      - Number any out-of-band management ports (if any) last.  It will be up to the
        Device Adapter developer whether to expose these to openflow or not. If you do
        not expose them, but do have the ports, still reserve the appropriate number of
        port numbers just in case.
    """
    # HTTP shortcuts
    HELLO_URI = '/restconf/adtran-hello:hello'

    # RPC XML shortcuts
    RESTART_RPC = '<system-restart xmlns="urn:ietf:params:xml:ns:yang:ietf-system"/>'

    def __init__(self, **kwargs):
        from net.pio_zmq import DEFAULT_PIO_TCP_PORT
        from net.pon_zmq import DEFAULT_PON_AGENT_TCP_PORT

        super(AdtranDeviceHandler, self).__init__()

        adapter = kwargs['adapter']
        device_id = kwargs['device-id']
        timeout = kwargs.get('timeout', 20)

        self.adapter = adapter
        self.adapter_agent = adapter.adapter_agent
        self.device_id = device_id
        self.log = structlog.get_logger(device_id=device_id)
        self.startup = None  # Startup/reboot deferred
        self.channel = None  # Proxy messaging channel with 'send' method
        self.logical_device_id = None
        self.pm_metrics = None
        self.alarms = None
        self.multicast_vlans = [DEFAULT_MULTICAST_VLAN]
        self.untagged_vlan = DEFAULT_UNTAGGED_VLAN
        self.utility_vlan = DEFAULT_UTILITY_VLAN
        self.default_mac_addr = '00:13:95:00:00:00'
        self._rest_support = None

        # Northbound and Southbound ports
        self.northbound_ports = {}  # port number -> Port
        self.southbound_ports = {}  # port number -> Port  (For PON, use pon-id as key)
        # self.management_ports = {}  # port number -> Port   TODO: Not currently supported

        self.num_northbound_ports = None
        self.num_southbound_ports = None
        # self.num_management_ports = None

        self.ip_address = None
        self.timeout = timeout
        self.restart_failure_timeout = 5 * 60   # 5 Minute timeout

        # REST Client
        self.rest_port = _DEFAULT_RESTCONF_PORT
        self.rest_username = _DEFAULT_RESTCONF_USERNAME
        self.rest_password = _DEFAULT_RESTCONF_PASSWORD
        self._rest_client = None

        # NETCONF Client
        self.netconf_port = _DEFAULT_NETCONF_PORT
        self.netconf_username = _DEFAULT_NETCONF_USERNAME
        self.netconf_password = _DEFAULT_NETCONF_PASSWORD
        self._netconf_client = None

        self.max_nni_ports = 1  # TODO: This is a VOLTHA imposed limit in 'flow_decomposer.py
                                # and logical_device_agent.py
        # OMCI ZMQ Channel
        self.pon_agent_port = DEFAULT_PON_AGENT_TCP_PORT
        self.pio_port = DEFAULT_PIO_TCP_PORT

        # Heartbeat support
        self.heartbeat_count = 0
        self.heartbeat_miss = 0
        self.heartbeat_interval = 2  # TODO: Decrease before release or any scale testing
        self.heartbeat_failed_limit = 3
        self.heartbeat_timeout = 5
        self.heartbeat = None
        self.heartbeat_last_reason = ''

        # Virtualized OLT Support
        self.is_virtual_olt = False

        # Installed flows
        self._evcs = {}  # Flow ID/name -> FlowEntry

    def _delete_logical_device(self):
        ldi, self.logical_device_id = self.logical_device_id, None

        if ldi is None:
            return

        self.log.debug('delete-logical-device', ldi=ldi)

        logical_device = self.adapter_agent.get_logical_device(ldi)
        self.adapter_agent.delete_logical_device(logical_device)

        device = self.adapter_agent.get_device(self.device_id)
        device.parent_id = ''

        #  Update the logical device mapping
        if ldi in self.adapter.logical_device_id_to_root_device_id:
            del self.adapter.logical_device_id_to_root_device_id[ldi]

    def __del__(self):
        # Kill any startup or heartbeat defers

        d, self.startup = self.startup, None
        h, self.heartbeat = self.heartbeat, None

        if d is not None and not d.called:
            d.cancel()

        if h is not None and not h.called:
            h.cancel()

        # Remove the logical device
        self._delete_logical_device()

        self.northbound_ports.clear()
        self.southbound_ports.clear()

    def __str__(self):
        return "AdtranDeviceHandler: {}".format(self.ip_address)

    @property
    def netconf_client(self):
        return self._netconf_client

    @property
    def rest_client(self):
        return self._rest_client

    @property
    def evcs(self):
        return list(self._evcs.values())

    def add_evc(self, evc):
        if self._evcs is not None and evc.name not in self._evcs:
            self._evcs[evc.name] = evc

    def remove_evc(self, evc):
        if self._evcs is not None and evc.name in self._evcs:
            del self._evcs[evc.name]

    def parse_provisioning_options(self, device):
        from net.pon_zmq import DEFAULT_PON_AGENT_TCP_PORT
        from net.pio_zmq import DEFAULT_PIO_TCP_PORT

        if not device.ipv4_address:
            self.activate_failed(device, 'No ip_address field provided')

        self.ip_address = device.ipv4_address

        #############################################################
        # Now optional parameters

        def check_tcp_port(value):
            ivalue = int(value)
            if ivalue <= 0 or ivalue > 65535:
                raise argparse.ArgumentTypeError("%s is a not a valid port number" % value)
            return ivalue

        def check_vid(value):
            ivalue = int(value)
            if ivalue <= 1 or ivalue > 4094:
                raise argparse.ArgumentTypeError("Valid VLANs are 2..4094")
            return ivalue

        parser = argparse.ArgumentParser(description='Adtran Device Adapter')
        parser.add_argument('--nc_username', '-u', action='store', default=_DEFAULT_NETCONF_USERNAME,
                            help='NETCONF username')
        parser.add_argument('--nc_password', '-p', action='store', default=_DEFAULT_NETCONF_PASSWORD,
                            help='NETCONF Password')
        parser.add_argument('--nc_port', '-t', action='store', default=_DEFAULT_NETCONF_PORT, type=check_tcp_port,
                            help='NETCONF TCP Port')
        parser.add_argument('--rc_username', '-U', action='store', default=_DEFAULT_RESTCONF_USERNAME,
                            help='REST username')
        parser.add_argument('--rc_password', '-P', action='store', default=_DEFAULT_RESTCONF_PASSWORD,
                            help='REST Password')
        parser.add_argument('--rc_port', '-T', action='store', default=_DEFAULT_RESTCONF_PORT, type=check_tcp_port,
                            help='RESTCONF TCP Port')
        parser.add_argument('--zmq_port', '-z', action='store', default=DEFAULT_PON_AGENT_TCP_PORT,
                            type=check_tcp_port, help='PON Agent ZeroMQ Port')
        parser.add_argument('--pio_port', '-Z', action='store', default=DEFAULT_PIO_TCP_PORT,
                            type=check_tcp_port, help='PIO Service ZeroMQ Port')
        parser.add_argument('--multicast_vlan', '-M', action='store',
                            default='{}'.format(DEFAULT_MULTICAST_VLAN),
                            help='Multicast VLAN'),
        parser.add_argument('--untagged_vlan', '-v', action='store',
                            default='{}'.format(DEFAULT_UNTAGGED_VLAN),
                            help='VLAN for Untagged Frames from ONUs'),
        parser.add_argument('--utility_vlan', '-B', action='store',
                            default='{}'.format(DEFAULT_UTILITY_VLAN),
                            help='VLAN for Untagged Frames from ONUs')
        parser.add_argument('--deprecated_option', '-X', action='store',
                            default='not-used', help='Deprecated command')
        try:
            args = parser.parse_args(shlex.split(device.extra_args))

            # May have multiple multicast VLANs
            self.multicast_vlans = [int(vid.strip()) for vid in args.multicast_vlan.split(',')]

            self.netconf_username = args.nc_username
            self.netconf_password = args.nc_password
            self.netconf_port = args.nc_port

            self.rest_username = args.rc_username
            self.rest_password = args.rc_password
            self.rest_port = args.rc_port

            self.pon_agent_port = args.zmq_port
            self.pio_port = args.pio_port

            if not self.rest_username:
                self.rest_username = 'NDE0NDRkNDk0ZQ==\n'.\
                    decode('base64').decode('hex')
            if not self.rest_password:
                self.rest_password = 'NTA0MTUzNTM1NzRmNTI0NA==\n'.\
                    decode('base64').decode('hex')
            if not self.netconf_username:
                self.netconf_username = 'Njg3Mzc2NzI2ZjZmNzQ=\n'.\
                    decode('base64').decode('hex')
            if not self.netconf_password:
                self.netconf_password = 'NDI0ZjUzNDM0Zg==\n'.\
                    decode('base64').decode('hex')

        except argparse.ArgumentError as e:
            self.activate_failed(device,
                                 'Invalid arguments: {}'.format(e.message),
                                 reachable=False)
        except Exception as e:
            self.log.exception('option_parsing_error: {}'.format(e.message))

    @inlineCallbacks
    def activate(self, device, done_deferred=None, reconciling=False):
        """
        Activate the OLT device

        :param device: A voltha.Device object, with possible device-type
                       specific extensions.
        :param done_deferred: (Deferred) Deferred to fire when done
        :param reconciling: If True, this adapter is taking over for a previous adapter
                            for an existing OLT
        """
        self.log.info('AdtranDeviceHandler.activating', reconciling=reconciling)

        if self.logical_device_id is None:
            try:
                # Parse our command line options for this device
                self.parse_provisioning_options(device)

                ############################################################################
                # Currently, only virtual OLT (pizzabox) is supported
                # self.is_virtual_olt = Add test for MOCK Device if we want to support it

                ############################################################################
                # Start initial discovery of NETCONF support (if any)
                try:
                    self.startup = self.make_netconf_connection()
                    yield self.startup

                except Exception as e:
                    self.log.exception('netconf-connection', e=e)
                    self.activate_failed(device, e.message, reachable=False)

                ############################################################################
                # Update access information on network device for full protocol support
                try:
                    self.startup = self.ready_network_access()
                    yield self.startup

                except Exception as e:
                    self.log.exception('network-setup', e=e)
                    self.activate_failed(device, e.message, reachable=False)

                ############################################################################
                # Restconf setup
                try:
                    self.startup = self.make_restconf_connection()
                    yield self.startup

                except Exception as e:
                    self.log.exception('restconf-setup', e=e)
                    self.activate_failed(device, e.message, reachable=False)

                ############################################################################
                # Get the device Information

                if reconciling:
                    device.connect_status = ConnectStatus.REACHABLE
                    self.adapter_agent.update_device(device)
                else:
                    try:
                        self.startup = self.get_device_info(device)
                        results = yield self.startup

                        device.model = results.get('model', 'unknown')
                        device.hardware_version = results.get('hardware_version', 'unknown')
                        device.firmware_version = results.get('firmware_version', 'unknown')
                        device.serial_number = results.get('serial_number', 'unknown')
                        device.images.image.extend(results.get('software-images', []))

                        device.root = True
                        device.vendor = results.get('vendor', 'Adtran, Inc.')
                        device.connect_status = ConnectStatus.REACHABLE
                        self.adapter_agent.update_device(device)

                    except Exception as e:
                        self.log.exception('device-info', e=e)
                        self.activate_failed(device, e.message, reachable=False)

                try:
                    # Enumerate and create Northbound NNI interfaces

                    device.reason = 'Enumerating NNI Interfaces'
                    self.adapter_agent.update_device(device)
                    self.startup = self.enumerate_northbound_ports(device)
                    results = yield self.startup

                    self.startup = self.process_northbound_ports(device, results)
                    yield self.startup

                    device.reason = 'Adding NNI Interfaces to Adapter'
                    self.adapter_agent.update_device(device)

                    if not reconciling:
                        for port in self.northbound_ports.itervalues():
                            self.adapter_agent.add_port(device.id, port.get_port())

                except Exception as e:
                    self.log.exception('NNI-enumeration', e=e)
                    self.activate_failed(device, e.message)

                try:
                    # Enumerate and create southbound interfaces

                    device.reason = 'Enumerating PON Interfaces'
                    self.adapter_agent.update_device(device)
                    self.startup = self.enumerate_southbound_ports(device)
                    results = yield self.startup

                    self.startup = self.process_southbound_ports(device, results)
                    yield self.startup

                    device.reason = 'Adding PON Interfaces to Adapter'
                    self.adapter_agent.update_device(device)

                    if not reconciling:
                        for port in self.southbound_ports.itervalues():
                            self.adapter_agent.add_port(device.id, port.get_port())

                except Exception as e:
                    self.log.exception('PON_enumeration', e=e)
                    self.activate_failed(device, e.message)

                if reconciling:
                    if device.admin_state == AdminState.ENABLED:
                        if device.parent_id:
                            self.logical_device_id = device.parent_id
                            self.adapter_agent.reconcile_logical_device(device.parent_id)
                        else:
                            self.log.info('no-logical-device-set')

                    # Reconcile child devices
                    self.adapter_agent.reconcile_child_devices(device.id)
                    ld_initialized = self.adapter_agent.get_logical_device()
                    assert device.parent_id == ld_initialized.id, \
                        'parent ID not Logical device ID'

                else:
                    # Complete activation by setting up logical device for this OLT and saving
                    # off the devices parent_id

                    ld_initialized = self.create_logical_device(device)

                ############################################################################
                # Setup PM configuration for this device
                try:
                    device.reason = 'Setting up PM configuration'
                    self.adapter_agent.update_device(device)

                    self.pm_metrics = OltPmMetrics(self, device, grouped=True, freq_override=False)
                    pm_config = self.pm_metrics.make_proto()
                    self.log.info("initial-pm-config", pm_config=pm_config)
                    self.adapter_agent.update_device_pm_config(pm_config, init=True)

                except Exception as e:
                    self.log.exception('pm-setup', e=e)
                    self.activate_failed(device, e.message)

                ############################################################################
                # Setup Alarm handler

                device.reason = 'Setting up Adapter Alarms'
                self.adapter_agent.update_device(device)

                self.alarms = AdapterAlarms(self.adapter, device.id)

                ############################################################################
                # Set the ports in a known good initial state
                if not reconciling:
                    try:
                        for port in self.northbound_ports.itervalues():
                            self.startup = yield port.reset()

                        for port in self.southbound_ports.itervalues():
                            self.startup = yield port.reset()

                    except Exception as e:
                        self.log.exception('port-reset', e=e)
                        self.activate_failed(device, e.message)

                ############################################################################
                # Create logical ports for all southbound and northbound interfaces
                try:
                    device.reason = 'Creating logical ports'
                    self.adapter_agent.update_device(device)
                    self.startup = self.create_logical_ports(device, ld_initialized, reconciling)
                    yield self.startup

                except Exception as e:
                    self.log.exception('logical-port', e=e)
                    self.activate_failed(device, e.message)

                ############################################################################
                # Register for ONU detection
                # self.adapter_agent.register_for_onu_detect_state(device.id)

                # Complete device specific steps
                try:
                    self.log.debug('device-activation-procedures')
                    self.startup = self.complete_device_specific_activation(device, reconciling)
                    yield self.startup

                except Exception as e:
                    self.log.exception('device-activation-procedures', e=e)
                    self.activate_failed(device, e.message)

                # Schedule the heartbeat for the device

                self.log.debug('starting-heartbeat')
                self.start_heartbeat(delay=10)

                device = self.adapter_agent.get_device(device.id)
                device.parent_id = ld_initialized.id
                device.oper_status = OperStatus.ACTIVE
                device.reason = ''
                self.adapter_agent.update_device(device)
                self.logical_device_id = ld_initialized.id

                # Start collecting stats from the device after a brief pause
                reactor.callLater(10, self.start_kpi_collection, device.id)

                # Signal completion
                self.log.info('activated')

            except Exception as e:
                self.log.exception('activate', e=e)
                if done_deferred is not None:
                    done_deferred.errback(e)
                raise

        if done_deferred is not None:
            done_deferred.callback('activated')

        returnValue(done_deferred)

    @inlineCallbacks
    def ready_network_access(self):
        # Override in device specific class if needed
        returnValue('nop')

    def activate_failed(self, device, reason, reachable=True):
        """
        Activation process (adopt_device) has failed.

        :param device:  A voltha.Device object, with possible device-type
                        specific extensions. Such extensions shall be described as part of
                        the device type specification returned by device_types().
        :param reason: (string) failure reason
        :param reachable: (boolean) Flag indicating if device may be reachable
                                    via RESTConf or NETConf even after this failure.
        """
        device.oper_status = OperStatus.FAILED
        if not reachable:
            device.connect_status = ConnectStatus.UNREACHABLE

        device.reason = reason
        self.adapter_agent.update_device(device)
        raise Exception('Failed to activate OLT: {}'.format(device.reason))

    @inlineCallbacks
    def make_netconf_connection(self, connect_timeout=None,
                                close_existing_client=False):

        if close_existing_client and self._netconf_client is not None:
            try:
                yield self._netconf_client.close()
            except:
                pass
            self._netconf_client = None

        client = self._netconf_client

        if client is None:
            if not self.is_virtual_olt:
                client = AdtranNetconfClient(self.ip_address,
                                             self.netconf_port,
                                             self.netconf_username,
                                             self.netconf_password,
                                             self.timeout)
            else:
                from voltha.adapters.adtran_olt.net.mock_netconf_client import MockNetconfClient
                client = MockNetconfClient(self.ip_address,
                                           self.netconf_port,
                                           self.netconf_username,
                                           self.netconf_password,
                                           self.timeout)
        if client.connected:
            self._netconf_client = client
            returnValue(True)

        timeout = connect_timeout or self.timeout

        try:
            request = client.connect(timeout)
            results = yield request
            self._netconf_client = client
            returnValue(results)

        except Exception as e:
            self.log.exception('Failed to create NETCONF Client', e=e)
            self._netconf_client = None
            raise

    @inlineCallbacks
    def make_restconf_connection(self, get_timeout=None):
        client = self._rest_client

        if client is None:
            client = AdtranRestClient(self.ip_address,
                                      self.rest_port,
                                      self.rest_username,
                                      self.rest_password,
                                      self.timeout)

        timeout = get_timeout or self.timeout

        try:
            request = client.request('GET', self.HELLO_URI, name='hello', timeout=timeout)
            results = yield request
            if isinstance(results, dict) and 'module-info' in results:
                self._rest_client = client
                returnValue(results)
            else:
                from twisted.internet.error import ConnectError
                self._rest_client = None
                raise ConnectError(string='Results received but unexpected data type or contents')
        except Exception:
            self._rest_client = None
            raise

    def create_logical_device(self, device):
        version = device.images.image[0].version

        ld = LogicalDevice(
            # NOTE: not setting id and datapath_id will let the adapter agent pick id
            desc=ofp_desc(mfr_desc=device.vendor,
                          hw_desc=device.hardware_version,
                          sw_desc=version,
                          serial_num=device.serial_number,
                          dp_desc='n/a'),
            switch_features=ofp_switch_features(n_buffers=256,
                                                n_tables=2,
                                                capabilities=(
                                                    OFPC_FLOW_STATS |
                                                    OFPC_TABLE_STATS |
                                                    OFPC_GROUP_STATS |
                                                    OFPC_PORT_STATS)),
            root_device_id=device.id)

        ld_initialized = self.adapter_agent.create_logical_device(ld,
                                                                  dpid=self.default_mac_addr)
        return ld_initialized

    @inlineCallbacks
    def create_logical_ports(self, device, ld_initialized, reconciling):
        if not reconciling:
            # Add the ports to the logical device

            for port in self.northbound_ports.itervalues():
                lp = port.get_logical_port()
                if lp is not None:
                    self.adapter_agent.add_logical_port(ld_initialized.id, lp)

            for port in self.southbound_ports.itervalues():
                lp = port.get_logical_port()
                if lp is not None:
                    self.adapter_agent.add_logical_port(ld_initialized.id, lp)

            # Clean up all EVCs, EVC maps and ACLs (exceptions are ok)
            try:
                from flow.evc import EVC
                self.startup = yield EVC.remove_all(self.netconf_client)
                from flow.utility_evc import UtilityEVC
                self.startup = yield UtilityEVC.remove_all(self.netconf_client)
                from flow.untagged_evc import UntaggedEVC
                self.startup = yield UntaggedEVC.remove_all(self.netconf_client)

            except Exception as e:
                self.log.exception('evc-cleanup', e=e)

            try:
                from flow.evc_map import EVCMap
                self.startup = yield EVCMap.remove_all(self.netconf_client)

            except Exception as e:
                self.log.exception('evc-map-cleanup', e=e)

            from flow.acl import ACL
            ACL.clear_all(device.id)
            try:
                self.startup = yield ACL.remove_all(self.netconf_client)

            except Exception as e:
                self.log.exception('acl-cleanup', e=e)

            from flow.flow_entry import FlowEntry
            FlowEntry.clear_all(device.id)

        # Start/stop the interfaces as needed. These are deferred calls

        dl = []
        for port in self.northbound_ports.itervalues():
            try:
                dl.append(port.start())
            except Exception as e:
                self.log.exception('northbound-port-startup', e=e)

        for port in self.southbound_ports.itervalues():
            try:
                dl.append(port.start() if port.admin_state == AdminState.ENABLED else port.stop())

            except Exception as e:
                self.log.exception('southbound-port-startup', e=e)

        results = yield defer.gatherResults(dl, consumeErrors=True)

        returnValue(results)

    @inlineCallbacks
    def device_information(self, device):
        """
        Examine the various managment models and extract device information for
        VOLTHA use

        :param device: A voltha.Device object, with possible device-type
                specific extensions.
        :return: (Deferred or None).
        """
        yield defer.Deferred(lambda c: c.callback("Not Required"))

    @inlineCallbacks
    def enumerate_northbound_ports(self, device):
        """
        Enumerate all northbound ports of a device. You should override
        this method in your derived class as necessary. Should you encounter
        a non-recoverable error, throw an appropriate exception.

        :param device: A voltha.Device object, with possible device-type
                specific extensions.
        :return: (Deferred or None).
        """
        yield defer.Deferred(lambda c: c.callback("Not Required"))

    @inlineCallbacks
    def process_northbound_ports(self, device, results):
        """
        Process the results from the 'enumerate_northbound_ports' method.
        You should override this method in your derived class as necessary and
        create an NNI Port object (of your own choosing) that supports a 'get_port'
        method. Once created, insert it into this base class's northbound_ports
        collection.

        Should you encounter a non-recoverable error, throw an appropriate exception.

        :param device: A voltha.Device object, with possible device-type
                specific extensions.
        :param results: Results from the 'enumerate_northbound_ports' method that
                you implemented. The type and contents are up to you to
        :return:
        """
        yield defer.Deferred(lambda c: c.callback("Not Required"))

    @inlineCallbacks
    def enumerate_southbound_ports(self, device):
        """
        Enumerate all southbound ports of a device. You should override
        this method in your derived class as necessary. Should you encounter
        a non-recoverable error, throw an appropriate exception.

        :param device: A voltha.Device object, with possible device-type
                specific extensions.
        :return: (Deferred or None).
        """
        yield defer.Deferred(lambda c: c.callback("Not Required"))

    @inlineCallbacks
    def process_southbound_ports(self, device, results):
        """
        Process the results from the 'enumerate_southbound_ports' method.
        You should override this method in your derived class as necessary and
        create an Port object (of your own choosing) that supports a 'get_port'
        method. Once created, insert it into this base class's southbound_ports
        collection.

        Should you encounter a non-recoverable error, throw an appropriate exception.

        :param device: A voltha.Device object, with possible device-type
                specific extensions.
        :param results: Results from the 'enumerate_southbound_ports' method that
                you implemented. The type and contents are up to you to
        :return:
        """
        yield defer.Deferred(lambda c: c.callback("Not Required"))

    # TODO: Move some of the items below from here and the EVC to a utility class

    def is_nni_port(self, port):
        return port in self.northbound_ports

    def is_uni_port(self, port):
        raise NotImplementedError('implement in derived class')

    def is_pon_port(self, port):
        raise NotImplementedError('implement in derived class')

    def is_logical_port(self, port):
        return not self.is_nni_port(port) and not self.is_uni_port(port) and not self.is_pon_port(port)

    def get_port_name(self, port):
        raise NotImplementedError('implement in derived class')

    @inlineCallbacks
    def complete_device_specific_activation(self, _device, _reconciling):
        # NOTE: Override this in your derived class for any device startup completion
        return defer.succeed('NOP')

    def disable(self):
        """
        This is called when a previously enabled device needs to be disabled based on a NBI call.
        """
        self.log.info('disabling', device_id=self.device_id)

        # Cancel any running enable/disable/... in progress
        d, self.startup = self.startup, None
        try:
            if d is not None and not d.called:
                d.cancel()
        except:
            pass
        # Get the latest device reference
        device = self.adapter_agent.get_device(self.device_id)

        # Drop registration for ONU detection
        # self.adapter_agent.unregister_for_onu_detect_state(self.device.id)

        # Suspend any active healthchecks / pings

        h, self.heartbeat = self.heartbeat, None
        try:
            if h is not None and not h.called:
                h.cancel()
        except:
            pass
        # Update the operational status to UNKNOWN

        device.oper_status = OperStatus.UNKNOWN
        device.connect_status = ConnectStatus.UNREACHABLE
        self.adapter_agent.update_device(device)

        # Disable all child devices first
        self.adapter_agent.update_child_devices_state(self.device_id,
                                                      admin_state=AdminState.DISABLED)

        # Remove the peer references from this device
        self.adapter_agent.delete_all_peer_references(self.device_id)

        # Remove the logical device
        self._delete_logical_device()

        # Set all ports to disabled
        self.adapter_agent.disable_all_ports(self.device_id)

        dl = []
        for port in self.northbound_ports.itervalues():
            dl.append(port.stop())

        for port in self.southbound_ports.itervalues():
            dl.append(port.stop())

        # NOTE: Flows removed before this method is called
        # Wait for completion

        self.startup = defer.gatherResults(dl, consumeErrors=True)

        def _drop_netconf():
            return self.netconf_client.close() if \
                self.netconf_client is not None else defer.succeed('NOP')

        def _null_clients():
            self._netconf_client = None
            self._rest_client = None

        # Shutdown communications with OLT

        self.startup.addCallbacks(_drop_netconf, _null_clients)
        self.startup.addCallbacks(_null_clients, _null_clients)

        self.log.info('disabled', device_id=device.id)
        return self.startup

    @inlineCallbacks
    def reenable(self, done_deferred=None):
        """
        This is called when a previously disabled device needs to be enabled based on a NBI call.
        :param done_deferred: (Deferred) Deferred to fire when done
        """
        self.log.info('re-enabling', device_id=self.device_id)

        # Cancel any running enable/disable/... in progress
        d, self.startup = self.startup, None
        try:
            if d is not None and not d.called:
                d.cancel()
        except:
            pass
        # Get the latest device reference
        device = self.adapter_agent.get_device(self.device_id)

        # Update the connect status to REACHABLE
        device.connect_status = ConnectStatus.REACHABLE
        device.oper_status = OperStatus.ACTIVATING
        self.adapter_agent.update_device(device)

        # Reenable any previously configured southbound ports
        for port in self.southbound_ports.itervalues():
            self.log.debug('reenable-checking-pon-port', pon_id=port.pon_id)

            gpon_info = self.get_xpon_info(port.pon_id)
            if gpon_info is not None and \
                gpon_info['channel-terminations'] is not None and \
                len(gpon_info['channel-terminations']) > 0:

                cterms = gpon_info['channel-terminations']
                if any(term.get('enabled') for term in cterms.itervalues()):
                    self.log.info('reenable', pon_id=port.pon_id)
                    port.enabled = True

        # Flows should not exist on re-enable. They are re-pushed
        if len(self._evcs):
            self.log.warn('evcs-found', evcs=self._evcs)
        self._evcs.clear()

        try:
            yield self.make_restconf_connection()

        except Exception as e:
            self.log.exception('adtran-hello-reconnect', e=e)

        try:
            yield self.make_netconf_connection()

        except Exception as e:
            self.log.exception('NETCONF-re-connection', e=e)

        # Recreate the logical device
        # NOTE: This causes a flow update event
        ld_initialized = self.create_logical_device(device)

        # Create logical ports for all southbound and northbound interfaces
        try:
            self.startup = self.create_logical_ports(device, ld_initialized, False)
            yield self.startup

        except Exception as e:
            self.log.exception('logical-port-creation', e=e)

        device = self.adapter_agent.get_device(device.id)
        device.parent_id = ld_initialized.id
        device.oper_status = OperStatus.ACTIVE
        device.reason = ''
        self.logical_device_id = ld_initialized.id

        # update device active status now
        self.adapter_agent.update_device(device)

        # Reenable all child devices
        self.adapter_agent.update_child_devices_state(device.id,
                                                      admin_state=AdminState.ENABLED)

        # Re-subscribe for ONU detection
        # self.adapter_agent.register_for_onu_detect_state(self.device.id)

        # TODO:
        # 1) Restart health check / pings

        self.log.info('re-enabled', device_id=device.id)

        if done_deferred is not None:
            done_deferred.callback('Done')

        returnValue('reenabled')

    @inlineCallbacks
    def reboot(self):
        """
        This is called to reboot a device based on a NBI call.  The admin state of the device
        will not change after the reboot.
        """
        self.log.debug('reboot')

        # Cancel any running enable/disable/... in progress
        d, self.startup = self.startup, None
        try:
            if d is not None and not d.called:
                d.cancel()
        except:
            pass
        # Issue reboot command

        if not self.is_virtual_olt:
            try:
                yield self.netconf_client.rpc(AdtranDeviceHandler.RESTART_RPC)

            except Exception as e:
                self.log.exception('NETCONF-shutdown', e=e)
                returnValue(defer.fail(Failure()))

        # self.adapter_agent.unregister_for_onu_detect_state(self.device.id)

        # Update the operational status to ACTIVATING and connect status to
        # UNREACHABLE

        device = self.adapter_agent.get_device(self.device_id)
        previous_oper_status = device.oper_status
        previous_conn_status = device.connect_status
        device.oper_status = OperStatus.ACTIVATING
        device.connect_status = ConnectStatus.UNREACHABLE
        self.adapter_agent.update_device(device)

        # Update the child devices connect state to UNREACHABLE
        self.adapter_agent.update_child_devices_state(self.device_id,
                                                      connect_status=ConnectStatus.UNREACHABLE)

        # Shutdown communications with OLT. Typically it takes about 2 seconds
        # or so after the reply before the restart actually occurs

        try:
            response = yield self.netconf_client.close()
            self.log.debug('Restart response XML was: {}'.format('ok' if response.ok else 'bad'))

        except Exception as e:
            self.log.exception('NETCONF-client-shutdown', e=e)

        # Clear off clients

        self._netconf_client = None
        self._rest_client = None

        # Run remainder of reboot process as a new task. The OLT then may be up in a
        # few moments or may take 3 minutes or more depending on any self tests enabled

        current_time = time.time()
        timeout = current_time + self.restart_failure_timeout

        self.startup = reactor.callLater(10, self._finish_reboot, timeout,
                                         previous_oper_status,
                                         previous_conn_status)
        returnValue(self.startup)

    @inlineCallbacks
    def _finish_reboot(self, timeout, previous_oper_status, previous_conn_status):
        # Now wait until REST & NETCONF are re-established or we timeout

        self.log.info('Resuming-activity',
                      remaining=timeout - time.time(), timeout=timeout, current=time.time())

        if self.rest_client is None:
            try:
                yield self.make_restconf_connection(get_timeout=10)

            except Exception:
                self.log.debug('No RESTCONF connection yet')
                self._rest_client = None

        if self.netconf_client is None:
            try:
                yield self.make_netconf_connection(connect_timeout=10)

            except Exception as e:
                try:
                    if self.netconf_client is not None:
                        yield self.netconf_client.close()
                except Exception as e:
                    self.log.exception(e.message)
                finally:
                    self._netconf_client = None

        if (self.netconf_client is None and not self.is_virtual_olt) or self.rest_client is None:
            current_time = time.time()
            if current_time < timeout:
                self.startup = reactor.callLater(5, self._finish_reboot, timeout,
                                                previous_oper_status,
                                                 previous_conn_status)
                returnValue(self.startup)

            if self.netconf_client is None and not self.is_virtual_olt:
                self.log.error('NETCONF-restore-failure')
                pass        # TODO: What is best course of action if cannot get clients back?

            if self.rest_client is None:
                self.log.error('RESTCONF-restore-failure')
                pass        # TODO: What is best course of action if cannot get clients back?

        # Pause additional 5 seconds to let allow OLT microservices to complete some more initialization
        yield asleep(5)
        # TODO: Update device info. The software images may have changed...
        # Get the latest device reference

        device = self.adapter_agent.get_device(self.device_id)
        device.oper_status = previous_oper_status
        device.connect_status = previous_conn_status
        self.adapter_agent.update_device(device)

        # Update the child devices connect state to REACHABLE
        self.adapter_agent.update_child_devices_state(self.device_id,
                                                      connect_status=ConnectStatus.REACHABLE)
        # Restart ports to previous state

        dl = []

        for port in self.northbound_ports.itervalues():
            dl.append(port.restart())

        for port in self.southbound_ports.itervalues():
            dl.append(port.restart())

        try:
            yield defer.gatherResults(dl, consumeErrors=True)
        except Exception as e:
            self.log.exception('port-restart', e=e)

        # Re-subscribe for ONU detection
        # self.adapter_agent.register_for_onu_detect_state(self.device.id)

        # Request reflow of any EVC/EVC-MAPs

        if len(self._evcs) > 0:
            dl = []
            for evc in self.evcs:
                dl.append(evc.reflow())

            try:
                yield defer.gatherResults(dl)
            except Exception as e:
                self.log.exception('flow-restart', e=e)

        self.log.info('rebooted', device_id=self.device_id)
        returnValue('Rebooted')

    @inlineCallbacks
    def delete(self):
        """
        This is called to delete a device from the PON based on a NBI call.
        If the device is an OLT then the whole PON will be deleted.
        """
        self.log.info('deleting', device_id=self.device_id)

        # Cancel any outstanding tasks

        d, self.startup = self.startup, None
        try:
            if d is not None and not d.called:
                d.cancel()
        except:
            pass
        h, self.heartbeat = self.heartbeat, None
        try:
            if h is not None and not h.called:
                h.cancel()
        except:
            pass
        # self.adapter_agent.unregister_for_onu_detect_state(self.device.id)

        # Remove all flows from the device
        # TODO: Create a bulk remove-all by device-id

        evcs = self._evcs
        self._evcs.clear()

        for evc in evcs:
            evc.delete()   # TODO: implement bulk-flow procedures

        # Remove all child devices
        self.adapter_agent.delete_all_child_devices(self.device_id)

        # Remove the logical device (should already be gone if disable came first)
        self._delete_logical_device()

        # Remove the peer references from this device
        self.adapter_agent.delete_all_peer_references(self.device_id)

        # Tell all ports to stop any background processing

        for port in self.northbound_ports.itervalues():
            port.delete()

        for port in self.southbound_ports.itervalues():
            port.delete()

        self.northbound_ports.clear()
        self.southbound_ports.clear()

        # Shutdown communications with OLT

        if self.netconf_client is not None:
            try:
                yield self.netconf_client.close()
            except Exception as e:
                self.log.exception('NETCONF-shutdown', e=e)

            self._netconf_client = None

        self._rest_client = None

        self.log.info('deleted', device_id=self.device_id)

    def packet_out(self, egress_port, msg):
        raise NotImplementedError('Overload in a derived class')

    def update_pm_config(self, device, pm_config):
        # TODO: This has not been tested
        self.log.info('update_pm_config', pm_config=pm_config)
        self.pm_metrics.update(pm_config)

    def start_kpi_collection(self, device_id):
        # TODO: This has not been tested
        def _collect(device_id, prefix):
            from voltha.protos.events_pb2 import KpiEvent, KpiEventType, MetricValuePairs

            try:
                # Step 1: gather metrics from device
                port_metrics = self.pm_metrics.collect_port_metrics()

                # Step 2: prepare the KpiEvent for submission
                # we can time-stamp them here or could use time derived from OLT
                ts = arrow.utcnow().timestamp
                kpi_event = KpiEvent(
                    type=KpiEventType.slice,
                    ts=ts,
                    prefixes={
                        prefix + '.{}'.format(k): MetricValuePairs(metrics=port_metrics[k])
                        for k in port_metrics.keys()}
                )
                # Step 3: submit
                self.adapter_agent.submit_kpis(kpi_event)

            except Exception as e:
                self.log.exception('failed-to-submit-kpis', e=e)

        self.pm_metrics.start_collector(_collect)

    @inlineCallbacks
    def get_device_info(self, device):
        """
        Perform an initial network operation to discover the device hardware
        and software version. Serial Number would be helpful as well.

        Upon successfully retrieving the information, remember to call the
        'start_heartbeat' method to keep in contact with the device being managed

        :param device: A voltha.Device object, with possible device-type
                specific extensions. Such extensions shall be described as part of
                the device type specification returned by device_types().
        """
        device = {}
        returnValue(device)

    def start_heartbeat(self, delay=10):
        assert delay > 1, 'Minimum heartbeat is 1 second'
        self.log.info('Starting-Device-Heartbeat ***')
        self.heartbeat = reactor.callLater(delay, self.check_pulse)
        return self.heartbeat

    def check_pulse(self):
        if self.logical_device_id is not None:
            try:
                self.heartbeat = self.rest_client.request('GET', self.HELLO_URI,
                                                          name='hello', timeout=5)
                self.heartbeat.addCallbacks(self._heartbeat_success, self._heartbeat_fail)

            except Exception as e:
                self.heartbeat = reactor.callLater(5, self._heartbeat_fail, e)

    def on_heatbeat_alarm(self, active):
        if active and self.netconf_client is None or not self.netconf_client.connected:
            self.make_netconf_connection(close_existing_client=True)

    def heartbeat_check_status(self, _):
        """
        Check the number of heartbeat failures against the limit and emit an alarm if needed
        """
        device = self.adapter_agent.get_device(self.device_id)

        try:
            from alarms.heartbeat_alarm import HeartbeatAlarm

            if self.heartbeat_miss >= self.heartbeat_failed_limit:
                if device.connect_status == ConnectStatus.REACHABLE:
                    self.log.warning('heartbeat-failed', count=self.heartbeat_miss)
                    device.connect_status = ConnectStatus.UNREACHABLE
                    device.oper_status = OperStatus.FAILED
                    device.reason = self.heartbeat_last_reason
                    self.adapter_agent.update_device(device)
                    HeartbeatAlarm(self, 'olt', self.heartbeat_miss).raise_alarm()
                    self.on_heatbeat_alarm(True)
            else:
                # Update device states
                if device.connect_status != ConnectStatus.REACHABLE:
                    device.connect_status = ConnectStatus.REACHABLE
                    device.oper_status = OperStatus.ACTIVE
                    device.reason = ''
                    self.adapter_agent.update_device(device)
                    HeartbeatAlarm(self, 'olt').clear_alarm()
                    self.on_heatbeat_alarm(False)

                if self.netconf_client is None or not self.netconf_client.connected:
                    self.make_netconf_connection(close_existing_client=True)

        except Exception as e:
            self.log.exception('heartbeat-check', e=e)

        # Reschedule next heartbeat
        if self.logical_device_id is not None:
            self.heartbeat_count += 1
            self.heartbeat = reactor.callLater(self.heartbeat_interval, self.check_pulse)

    def _heartbeat_success(self, results):
        self.log.debug('heartbeat-success')
        self.heartbeat_miss = 0
        self.heartbeat_last_reason = ''
        self.heartbeat_check_status(results)

    def _heartbeat_fail(self, failure):
        self.heartbeat_miss += 1
        self.log.info('heartbeat-miss', failure=failure,
                      count=self.heartbeat_count,
                      miss=self.heartbeat_miss)
        self.heartbeat_last_reason = 'RESTCONF connectivity error'
        self.heartbeat_check_status(None)

    @staticmethod
    def parse_module_revision(revision):
        try:
            return datetime.datetime.strptime(revision, '%Y-%m-%d')
        except Exception:
            return None
