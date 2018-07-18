#
# Copyright 2017 the original author or authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import structlog
from datetime import datetime, timedelta
from transitions import Machine
from twisted.internet import reactor
from voltha.extensions.omci.omci_frame import OmciFrame
from voltha.extensions.omci.omci_defs import EntityOperations, ReasonCodes, \
    AttributeAccess
from voltha.extensions.omci.omci_cc import OmciCCRxEvents, OMCI_CC, TX_REQUEST_KEY, \
    RX_RESPONSE_KEY
from voltha.extensions.omci.onu_device_entry import OnuDeviceEvents, OnuDeviceEntry, \
    SUPPORTED_MESSAGE_ENTITY_KEY, SUPPORTED_MESSAGE_TYPES_KEY
from voltha.extensions.omci.omci_entities import OntData
from common.event_bus import EventBusClient

RxEvent = OmciCCRxEvents
DevEvent = OnuDeviceEvents
OP = EntityOperations
RC = ReasonCodes
AA = AttributeAccess


class MibSynchronizer(object):
    """
    OpenOMCI MIB Synchronizer state machine
    """
    DEFAULT_STATES = ['disabled', 'starting', 'uploading', 'examining_mds',
                      'in_sync', 'out_of_sync', 'auditing', 'resynchronizing']

    DEFAULT_TRANSITIONS = [
        {'trigger': 'start', 'source': 'disabled', 'dest': 'starting'},

        {'trigger': 'upload_mib', 'source': 'starting', 'dest': 'uploading'},
        {'trigger': 'examine_mds', 'source': 'starting', 'dest': 'examining_mds'},

        {'trigger': 'success', 'source': 'uploading', 'dest': 'in_sync'},
        {'trigger': 'timeout', 'source': 'uploading', 'dest': 'starting'},

        {'trigger': 'success', 'source': 'examining_mds', 'dest': 'in_sync'},
        {'trigger': 'timeout', 'source': 'examining_mds', 'dest': 'starting'},
        {'trigger': 'mismatch', 'source': 'examining_mds', 'dest': 'uploading'},

        {'trigger': 'audit_mib', 'source': 'in_sync', 'dest': 'auditing'},
        {'trigger': 'audit_mib', 'source': 'out_of_sync', 'dest': 'auditing'},

        {'trigger': 'success', 'source': 'auditing', 'dest': 'in_sync'},
        {'trigger': 'timeout', 'source': 'auditing', 'dest': 'starting'},
        {'trigger': 'mismatch', 'source': 'auditing', 'dest': 'resynchronizing'},
        {'trigger': 'force_resync', 'source': 'auditing', 'dest': 'resynchronizing'},

        {'trigger': 'success', 'source': 'resynchronizing', 'dest': 'in_sync'},
        {'trigger': 'diffs_found', 'source': 'resynchronizing', 'dest': 'out_of_sync'},
        {'trigger': 'timeout', 'source': 'resynchronizing', 'dest': 'out_of_sync'},

        # Do wildcard 'stop' trigger last so it covers all previous states
        {'trigger': 'stop', 'source': '*', 'dest': 'disabled'},
    ]
    DEFAULT_TIMEOUT_RETRY = 5      # Seconds to delay after task failure/timeout
    DEFAULT_AUDIT_DELAY = 15       # Periodic tick to audit the MIB Data Sync
    DEFAULT_RESYNC_DELAY = 300     # Periodically force a resync

    def __init__(self, agent, device_id, mib_sync_tasks, db, states=DEFAULT_STATES,
                 transitions=DEFAULT_TRANSITIONS,
                 initial_state='disabled',
                 timeout_delay=DEFAULT_TIMEOUT_RETRY,
                 audit_delay=DEFAULT_AUDIT_DELAY,
                 resync_delay=DEFAULT_RESYNC_DELAY):
        """
        Class initialization

        :param agent: (OpenOmciAgent) Agent
        :param device_id: (str) ONU Device ID
        :param db: (MibDbVolatileDict) MIB Database
        :param mib_sync_tasks: (dict) Tasks to run
        :param states: (list) List of valid states
        :param transitions: (dict) Dictionary of triggers and state changes
        :param initial_state: (str) Initial state machine state
        :param timeout_delay: (int/float) Number of seconds after a timeout to attempt
                                          a retry (goes back to starting state)
        :param audit_delay: (int) Seconds between MIB audits while in sync. Set to
                                  zero to disable audit. An operator can request
                                  an audit manually by calling 'self.audit_mib'
        :param resync_delay: (int) Seconds in sync before performing a forced MIB
                                   resynchronization
        """
        self.log = structlog.get_logger(device_id=device_id)

        self._agent = agent
        self._device_id = device_id
        self._device = None
        self._database = db
        self._timeout_delay = timeout_delay
        self._audit_delay = audit_delay
        self._resync_delay = resync_delay

        self._upload_task = mib_sync_tasks['mib-upload']
        self._get_mds_task = mib_sync_tasks['get-mds']
        self._audit_task = mib_sync_tasks['mib-audit']
        self._resync_task = mib_sync_tasks['mib-resync']

        self._deferred = None
        self._current_task = None   # TODO: Support multiple running tasks after v.1.3.0 release
        self._task_deferred = None
        self._mib_data_sync = 0
        self._last_mib_db_sync_value = None
        self._device_in_db = False

        self._on_olt_only_diffs = None
        self._on_onu_only_diffs = None
        self._attr_diffs = None

        self._event_bus = EventBusClient()
        self._omci_cc_subscriptions = {               # RxEvent.enum -> Subscription Object
            RxEvent.MIB_Reset: None,
            RxEvent.AVC_Notification: None,
            RxEvent.MIB_Upload: None,
            RxEvent.MIB_Upload_Next: None,
            RxEvent.Create: None,
            RxEvent.Delete: None,
            RxEvent.Set: None,
        }
        self._omci_cc_sub_mapping = {
            RxEvent.MIB_Reset: self.on_mib_reset_response,
            RxEvent.AVC_Notification: self.on_avc_notification,
            RxEvent.MIB_Upload: self.on_mib_upload_response,
            RxEvent.MIB_Upload_Next: self.on_mib_upload_next_response,
            RxEvent.Create: self.on_create_response,
            RxEvent.Delete: self.on_delete_response,
            RxEvent.Set: self.on_set_response,
        }
        self._onu_dev_subscriptions = {               # DevEvent.enum -> Subscription Object
            DevEvent.OmciCapabilitiesEvent: None
        }
        self._onu_dev_sub_mapping = {
            DevEvent.OmciCapabilitiesEvent: self.on_capabilities_event
        }

        # Statistics and attributes
        # TODO: add any others if it will support problem diagnosis

        # Set up state machine to manage states
        self.machine = Machine(model=self, states=states,
                               transitions=transitions,
                               initial=initial_state,
                               queued=True,
                               name='{}-{}'.format(self.__class__.__name__,
                                                   device_id))

    def _cancel_deferred(self):
        d1, self._deferred = self._deferred, None
        d2, self._task_deferred = self._task_deferred, None

        for d in [d1, d1]:
            try:
                if d is not None and not d.called:
                    d.cancel()
            except:
                pass

    def __str__(self):
        return 'MIBSynchronizer: Device ID: {}, State:{}'.format(self._device_id, self.state)

    def delete(self):
        """
        Cleanup any state information
        """
        self.stop()
        db, self._database = self._database, None

        if db is not None:
            db.remove(self._device_id)

    @property
    def device_id(self):
        return self._device_id

    @property
    def mib_data_sync(self):
        return self._mib_data_sync

    def increment_mib_data_sync(self):
        self._mib_data_sync += 1
        if self._mib_data_sync > 255:
            self._mib_data_sync = 0

        if self._database is not None:
            self._database.save_mib_data_sync(self._device_id,
                                              self._mib_data_sync)

    @property
    def last_mib_db_sync(self):
        return self._last_mib_db_sync_value

    @last_mib_db_sync.setter
    def last_mib_db_sync(self, value):
        self._last_mib_db_sync_value = value
        if self._database is not None:
            self._database.save_last_sync(self.device_id, value)

    @property
    def is_new_onu(self):
        """
        Is this a new ONU (has never completed MIB synchronization)
        :return: (bool) True if this ONU should be considered new
        """
        return self.last_mib_db_sync is None

    def on_enter_disabled(self):
        """
        State machine is being stopped
        """
        self.log.debug('state-transition')

        self._cancel_deferred()
        if self._device is not None:
            self._device.mib_db_in_sync = False

        task, self._current_task = self._current_task, None
        if task is not None:
            task.stop()

        # Drop Response and Autonomous notification subscriptions
        for event, sub in self._omci_cc_subscriptions.iteritems():
            if sub is not None:
                self._omci_cc_subscriptions[event] = None
                self._device.omci_cc.event_bus.unsubscribe(sub)

        for event, sub in self._onu_dev_subscriptions.iteritems():
            if sub is not None:
                self._onu_dev_subscriptions[event] = None
                self._device.event_bus.unsubscribe(sub)

        # TODO: Stop and remove any currently running or scheduled tasks
        # TODO: Anything else?

    def _seed_database(self):
        if not self._device_in_db:
            try:
                try:
                    self._database.start()
                    self._database.add(self._device_id)
                    self.log.debug('seed-db-does-not-exist', device_id=self._device_id)

                except KeyError:
                    # Device already is in database
                    self.log.debug('seed-db-exist', device_id=self._device_id)
                    self._mib_data_sync = self._database.get_mib_data_sync(self._device_id)
                    self._last_mib_db_sync_value = self._database.get_last_sync(self._device_id)

                self._device_in_db = True

            except Exception as e:
                self.log.exception('seed-database-failure', e=e)

    def on_enter_starting(self):
        """
        Determine ONU status and start MIB Synchronization tasks
        """
        self._device = self._agent.get_device(self._device_id)
        self.log.debug('state-transition', new_onu=self.is_new_onu)

        # Make sure root of external MIB Database exists
        self._seed_database()

        # Set up Response and Autonomous notification subscriptions
        try:
            for event, sub in self._omci_cc_sub_mapping.iteritems():
                if self._omci_cc_subscriptions[event] is None:
                    self._omci_cc_subscriptions[event] = \
                        self._device.omci_cc.event_bus.subscribe(
                            topic=OMCI_CC.event_bus_topic(self._device_id, event),
                            callback=sub)

        except Exception as e:
            self.log.exception('omci-cc-subscription-setup', e=e)

        # Set up ONU device subscriptions
        try:
            for event, sub in self._onu_dev_sub_mapping.iteritems():
                if self._onu_dev_subscriptions[event] is None:
                    self._onu_dev_subscriptions[event] = \
                        self._device.event_bus.subscribe(
                                topic=OnuDeviceEntry.event_bus_topic(self._device_id, event),
                                callback=sub)

        except Exception as e:
            self.log.exception('dev-subscription-setup', e=e)

        # Determine if this ONU has ever synchronized
        if self.is_new_onu:
            # Start full MIB upload
            self._deferred = reactor.callLater(0, self.upload_mib)

        else:
            # Examine the MIB Data Sync
            self._deferred = reactor.callLater(0, self.examine_mds)

    def on_enter_uploading(self):
        """
        Begin full MIB data sync, starting with a MIB RESET
        """
        def success(results):
            self.log.debug('mib-upload-success: {}'.format(results))
            self._current_task = None
            self._deferred = reactor.callLater(0, self.success)

        def failure(reason):
            self.log.info('mib-upload-failure', reason=reason)
            self._current_task = None
            self._deferred = reactor.callLater(self._timeout_delay, self.timeout)

        self._device.mib_db_in_sync = False
        self._current_task = self._upload_task(self._agent, self._device_id)

        self._task_deferred = self._device.task_runner.queue_task(self._current_task)
        self._task_deferred.addCallbacks(success, failure)

    def on_enter_examining_mds(self):
        """
        Create a simple task to fetch the MIB Data Sync value and
        determine if the ONU value matches what is in the MIB database
        """
        self._mib_data_sync = self._database.get_mib_data_sync(self._device_id) or 0

        def success(onu_mds_value):
            self.log.debug('examine-mds-success: {}'.format(onu_mds_value))
            self._current_task = None

            # Examine MDS value
            if self.mib_data_sync == onu_mds_value:
                self._deferred = reactor.callLater(0, self.success)
            else:
                self._deferred = reactor.callLater(0, self.mismatch)

        def failure(reason):
            self.log.info('examine-mds-failure', reason=reason)
            self._current_task = None
            self._deferred = reactor.callLater(self._timeout_delay, self.timeout)

        self._device.mib_db_in_sync = False
        self._current_task = self._get_mds_task(self._agent, self._device_id)

        self._task_deferred = self._device.task_runner.queue_task(self._current_task)
        self._task_deferred.addCallbacks(success, failure)

    def on_enter_in_sync(self):
        """
        Schedule a tick to occur to in the future to request an audit
        """
        self.log.debug('state-transition', audit_delay=self._audit_delay)
        self.last_mib_db_sync = datetime.utcnow()
        self._device.mib_db_in_sync = True

        if self._audit_delay > 0:
            self._deferred = reactor.callLater(self._audit_delay, self.audit_mib)

    def on_enter_out_of_sync(self):
        """
        The MIB in OpenOMCI and the ONU are out of sync.  This can happen if:

           o the MIB_Data_Sync values are not equal, or
           o the MIBs were compared and differences were found.

        If all of the *_diff properties are allNone, then we are here after initial
        startup and MDS did not match, or the MIB Audit/Resync state failed.

        In the second case, one or more of our *_diff properties will be non-None.
        If that is true, we need to update the ONU accordingly.

        Schedule a tick to occur to in the future to request an audit
        """
        self.log.debug('state-transition', audit_delay=self._audit_delay)
        self._device.mib_db_in_sync = False

        if all(diff is None for diff in [self._on_olt_only_diffs,
                                         self._on_onu_only_diffs,
                                         self._attr_diffs]):
            # Retry the Audit process
            self._deferred = reactor.callLater(1, self.audit_mib)

        else:
            step = 'Nothing'
            class_id = 0
            instance_id = 0
            attribute = ''

            try:
                # Need to update the ONU accordingly
                if self._attr_diffs is not None:
                    step = 'attribute-update'
                    pass    # TODO: Perform the 'set' commands needed

                if self._on_onu_only_diffs is not None:
                    step = 'onu-cleanup'
                    #
                    # TODO: May want to watch for ONU only attributes
                    #    It is possible that if they are the 'default' value or
                    #    are not used if another attribute is set a specific way.
                    #
                    #    For instance, no one may set the gal_loopback_configuration
                    #    in the GEM Interworking Termination point since its default
                    #    values is '0' disable, but when we audit, the ONU will report zero.
                    #
                    #    A good way to perhaps fix this is to update our database with the
                    #    default.  Or perhaps set all defaults in the database in the first
                    #    place when we do the initial create/set.
                    #
                    pass  # TODO: Perform 'delete' commands as needed, see 'default' note above

                if self._on_olt_only_diffs is not None:
                    step = 'olt-push'
                    pass    # TODO: Perform 'create' commands as needed

                self._deferred = reactor.callLater(1, self.audit_mib)

            except Exception as e:
                self.log.exception('onu-update', e=e, step=step, class_id=class_id,
                                   instance_id=instance_id, attribute=attribute)
                # Retry the Audit process
                self._deferred = reactor.callLater(1, self.audit_mib)

    def on_enter_auditing(self):
        """
        Perform a MIB Audit.  If our last MIB resync was too long in the
        past, perform a resynchronization anyway
        """
        next_resync = self.last_mib_db_sync + timedelta(seconds=self._resync_delay)\
            if self.last_mib_db_sync is not None else datetime.utcnow()

        self.log.debug('state-transition', next_resync=next_resync)

        if datetime.utcnow() >= next_resync:
            self._deferred = reactor.callLater(0, self.force_resync)
        else:
            def success(onu_mds_value):
                self.log.debug('get-mds-success: {}'.format(onu_mds_value))
                self._current_task = None

                # Examine MDS value
                if self.mib_data_sync == onu_mds_value:
                    self._deferred = reactor.callLater(0, self.success)
                else:
                    self._device.mib_db_in_sync = False
                    self._deferred = reactor.callLater(0, self.mismatch)

            def failure(reason):
                self.log.info('get-mds-failure', reason=reason)
                self._current_task = None
                self._deferred = reactor.callLater(self._timeout_delay, self.timeout)

            self._current_task = self._audit_task(self._agent, self._device_id)
            self._task_deferred = self._device.task_runner.queue_task(self._current_task)
            self._task_deferred.addCallbacks(success, failure)

    def on_enter_resynchronizing(self):
        """
        Perform a resynchronization of the MIB database

        First calculate any differences
        """
        def success(results):
            self.log.debug('resync-success: {}'.format(results))

            on_olt_only = results.get('on-olt-only')
            on_onu_only = results.get('on-onu-only')
            attr_diffs = results.get('attr-diffs')

            self._current_task = None
            self._on_olt_only_diffs = on_olt_only if len(on_olt_only) else None
            self._on_onu_only_diffs = on_onu_only if len(on_onu_only) else None
            self._attr_diffs = attr_diffs if len(attr_diffs) else None

            if all(diff is None for diff in [self._on_olt_only_diffs,
                                             self._on_onu_only_diffs,
                                             self._attr_diffs]):
                # TODO: If here, do we need to make sure OpenOMCI mib_data_sync matches
                #       the ONU.  Remember we compared against an ONU snapshot, it may
                #       be different now.  Best thing to do is perhaps set it to our
                #       MDS value if different. Also remember that setting the MDS on
                #       the ONU to 'n' is a set command and it will be 'n+1' after the
                #       set.
                #
                # TODO: Also look into attributes covered by AVC and treat appropriately
                #       since may have missed the AVC

                self._deferred = reactor.callLater(0, self.success)
            else:
                self._deferred = reactor.callLater(0, self.diffs_found)

        def failure(reason):
            self.log.info('resync-failure', reason=reason)
            self._current_task = None
            self._on_olt_only_diffs = None
            self._on_onu_only_diffs = None
            self._attr_diffs = None
            self._deferred = reactor.callLater(self._timeout_delay, self.timeout)

        self._current_task = self._resync_task(self._agent, self._device_id)
        self._task_deferred = self._device.task_runner.queue_task(self._current_task)
        self._task_deferred.addCallbacks(success, failure)

    def on_mib_reset_response(self, _topic, msg):
        """
        Called upon receipt of a MIB Reset Response for this ONU

        :param _topic: (str) OMCI-RX topic
        :param msg: (dict) Dictionary with 'rx-response' and 'tx-request' (if any)
        """
        self.log.debug('on-mib-reset-response', state=self.state)
        try:
            response = msg[RX_RESPONSE_KEY]

            # Check if expected in current mib_sync state
            if self.state != 'uploading' or self._omci_cc_subscriptions[RxEvent.MIB_Reset] is None:
                self.log.error('rx-in-invalid-state', state=self.state)

            else:
                now = datetime.utcnow()

                if not isinstance(response, OmciFrame):
                    raise TypeError('Response should be an OmciFrame')

                omci_msg = response.fields['omci_message'].fields
                status = omci_msg['success_code']

                assert status == RC.Success, 'Unexpected MIB reset response status: {}'. \
                    format(status)

                self._device.mib_db_in_sync = False
                self._mib_data_sync = 0
                self._device._modified = now
                self._database.on_mib_reset(self._device_id)

        except KeyError:
            pass            # NOP

    def on_avc_notification(self, _topic, msg):
        """
        Process an Attribute Value Change Notification

        :param _topic: (str) OMCI-RX topic
        :param msg: (dict) Dictionary with 'rx-response' and 'tx-request' (if any)
        """
        self.log.debug('on-avc-notification', state=self.state)

        if self._omci_cc_subscriptions[RxEvent.AVC_Notification]:
            try:
                notification = msg[RX_RESPONSE_KEY]

                if self.state == 'disabled':
                    self.log.error('rx-in-invalid-state', state=self.state)

                elif self.state != 'uploading':
                    # Inspect the notification
                    omci_msg = notification.fields['omci_message'].fields
                    class_id = omci_msg['entity_class']
                    instance_id = omci_msg['entity_id']
                    data = omci_msg['data']
                    attributes = [data.keys()]

                    # Look up ME Instance in Database. Not-found can occur if a MIB
                    # reset has occurred
                    info = self._database.query(self.device_id, class_id, instance_id, attributes)
                    # TODO: Add old/new info to log message
                    self.log.debug('avc-change', class_id=class_id, instance_id=instance_id)

                    # Save the changed data to the MIB.
                    changed = self._database.set(self.device_id, class_id, instance_id, data)

                    if changed:
                        # Autonomous creation and deletion of managed entities do not
                        # result in an increment of the MIB data sync value. However,
                        # AVC's in response to a change by the Operator do incur an
                        # increment of the MIB Data Sync
                        pass

            except KeyError:
                pass            # NOP

    def on_mib_upload_response(self, _topic, msg):
        """
        Process a MIB Upload response

        :param _topic: (str) OMCI-RX topic
        :param msg: (dict) Dictionary with 'rx-response' and 'tx-request' (if any)
        """
        self.log.debug('on-mib-upload-next-response', state=self.state)

        if self._omci_cc_subscriptions[RxEvent.MIB_Upload]:
            # Check if expected in current mib_sync state
            if self.state == 'resynchronizing':
                # The resync task handles this
                # TODO: Remove this subscription if we never do anything with the response
                return

            if self.state != 'uploading':
                self.log.error('rx-in-invalid-state', state=self.state)

    def on_mib_upload_next_response(self, _topic, msg):
        """
        Process a MIB Upload Next response

        :param _topic: (str) OMCI-RX topic
        :param msg: (dict) Dictionary with 'rx-response' and 'tx-request' (if any)
        """
        self.log.debug('on-mib-upload-next-response', state=self.state)

        if self._omci_cc_subscriptions[RxEvent.MIB_Upload_Next]:
            try:
                if self.state == 'resynchronizing':
                    # The resync task handles this
                    return

                # Check if expected in current mib_sync state
                if self.state != 'uploading':
                    self.log.error('rx-in-invalid-state', state=self.state)

                else:
                    response = msg[RX_RESPONSE_KEY]

                    # Extract entity instance information
                    omci_msg = response.fields['omci_message'].fields

                    class_id = omci_msg['object_entity_class']
                    entity_id = omci_msg['object_entity_id']

                    # Filter out the 'mib_data_sync' from the database. We save that at
                    # the device level and do not want it showing up during a re-sync
                    # during data compares

                    if class_id == OntData.class_id:
                        return

                    attributes = {k: v for k, v in omci_msg['object_data'].items()}

                    # Save to the database
                    self._database.set(self._device_id, class_id, entity_id, attributes)

            except KeyError:
                pass            # NOP
            except Exception as e:
                self.log.exception('upload-next', e=e)

    def on_create_response(self, _topic, msg):
        """
        Process a Set response

        :param _topic: (str) OMCI-RX topic
        :param msg: (dict) Dictionary with 'rx-response' and 'tx-request' (if any)
        """
        self.log.debug('on-create-response', state=self.state)

        if self._omci_cc_subscriptions[RxEvent.Create]:
            if self.state in ['disabled', 'uploading']:
                self.log.error('rx-in-invalid-state', state=self.state)
                return
            try:
                request = msg[TX_REQUEST_KEY]
                response = msg[RX_RESPONSE_KEY]
                status = response.fields['omci_message'].fields['success_code']

                if status != RC.Success and status != RC.InstanceExists:
                    # TODO: Support offline ONTs in post VOLTHA v1.3.0
                    omci_msg = response.fields['omci_message']
                    self.log.warn('set-response-failure',
                                  class_id=omci_msg.fields['entity_class'],
                                  instance_id=omci_msg.fields['entity_id'],
                                  status=omci_msg.fields['success_code'],
                                  status_text=self._status_to_text(omci_msg.fields['success_code']),
                                  parameter_error_attributes_mask=omci_msg.fields['parameter_error_attributes_mask'])
                else:
                    omci_msg = request.fields['omci_message'].fields
                    class_id = omci_msg['entity_class']
                    entity_id = omci_msg['entity_id']
                    attributes = {k: v for k, v in omci_msg['data'].items()}

                    # Save to the database
                    created = self._database.set(self._device_id, class_id, entity_id, attributes)

                    if created:
                        self.increment_mib_data_sync()

                    # If the ME contains set-by-create or writeable values that were
                    # not specified in the create command, the ONU will have
                    # initialized those fields

                    if class_id in self._device.me_map:
                        sbc_w_set = {attr.field.name for attr in self._device.me_map[class_id].attributes
                                     if (AA.SBC in attr.access or AA.W in attr.access)
                                     and attr.field.name != 'managed_entity_id'}

                        missing = sbc_w_set - {k for k in attributes.iterkeys()}

                        if len(missing):
                            # Request the missing attributes
                            self.update_sbc_w_items(class_id, entity_id, missing)

            except KeyError as e:
                pass            # NOP

            except Exception as e:
                self.log.exception('create', e=e)

    def update_sbc_w_items(self, class_id, entity_id, missing_attributes):
        """
        Perform a get-request for Set-By-Create (SBC) or writable (w) attributes
        that were not specified in the original Create request.

        :param class_id: (int) Class ID
        :param entity_id: (int) Instance ID
        :param missing_attributes: (set) Missing SBC or Writable attribute
        """
        if len(missing_attributes) and class_id in self._device.me_map:
            from voltha.extensions.omci.tasks.omci_get_request import OmciGetRequest

            def success(results):
                self._database.set(self._device_id, class_id, entity_id, results.attributes)

            def failure(reason):
                self.log.warn('update-sbc-w-failed', reason=reason, class_id=class_id,
                              entity_id=entity_id, attributes=missing_attributes)

            d = self._device.task_runner.queue_task(OmciGetRequest(self._agent, self._device_id,
                                                                   self._device.me_map[class_id],
                                                                   entity_id, missing_attributes,
                                                                   allow_failure=True))
            d.addCallbacks(success, failure)

    def on_delete_response(self, _topic, msg):
        """
        Process a Delete response

        :param _topic: (str) OMCI-RX topic
        :param msg: (dict) Dictionary with 'rx-response' and 'tx-request' (if any)
        """
        self.log.debug('on-delete-response', state=self.state)

        if self._omci_cc_subscriptions[RxEvent.Delete]:
            if self.state in ['disabled', 'uploading']:
                self.log.error('rx-in-invalid-state', state=self.state)
                return
            try:
                request = msg[TX_REQUEST_KEY]
                response = msg[RX_RESPONSE_KEY]

                if response.fields['omci_message'].fields['success_code'] != RC.Success:
                    # TODO: Support offline ONTs in post VOLTHA v1.3.0
                    omci_msg = response.fields['omci_message']
                    self.log.warn('set-response-failure',
                                  class_id=omci_msg.fields['entity_class'],
                                  instance_id=omci_msg.fields['entity_id'],
                                  status=omci_msg.fields['success_code'],
                                  status_text=self._status_to_text(omci_msg.fields['success_code']))
                else:
                    omci_msg = request.fields['omci_message'].fields
                    class_id = omci_msg['entity_class']
                    entity_id = omci_msg['entity_id']

                    # Remove from the database
                    deleted = self._database.delete(self._device_id, class_id, entity_id)

                    if deleted:
                        self.increment_mib_data_sync()

            except KeyError as e:
                pass            # NOP
            except Exception as e:
                self.log.exception('delete', e=e)

    def on_set_response(self, _topic, msg):
        """
        Process a Set response

        :param _topic: (str) OMCI-RX topic
        :param msg: (dict) Dictionary with 'rx-response' and 'tx-request' (if any)
        """
        self.log.debug('on-set-response', state=self.state)

        if self._omci_cc_subscriptions[RxEvent.Set]:
            if self.state in ['disabled', 'uploading']:
                self.log.error('rx-in-invalid-state', state=self.state)
            try:
                request = msg[TX_REQUEST_KEY]
                response = msg[RX_RESPONSE_KEY]

                if response.fields['omci_message'].fields['success_code'] != RC.Success:
                    # TODO: Support offline ONTs in post VOLTHA v1.3.0
                    omci_msg = response.fields['omci_message']
                    self.log.warn('set-response-failure',
                                  class_id=omci_msg.fields['entity_class'],
                                  instance_id=omci_msg.fields['entity_id'],
                                  status=omci_msg.fields['success_code'],
                                  status_text=self._status_to_text(omci_msg.fields['success_code']),
                                  unsupported_attribute_mask=omci_msg.fields['unsupported_attributes_mask'],
                                  failed_attribute_mask=omci_msg.fields['failed_attributes_mask'])
                else:
                    omci_msg = request.fields['omci_message'].fields
                    class_id = omci_msg['entity_class']
                    entity_id = omci_msg['entity_id']
                    attributes = {k: v for k, v in omci_msg['data'].items()}

                    # Save to the database
                    modified = self._database.set(self._device_id, class_id, entity_id, attributes)

                    if modified:
                        self.increment_mib_data_sync()

            except KeyError as e:
                pass            # NOP
            except Exception as e:
                self.log.exception('set', e=e)
    def on_capabilities_event(self, _topic, msg):
        """
        Process a OMCI capabilties event
        :param _topic: (str) OnuDeviceEntry Capabilities event
        :param msg: (dict) Message Entities & Message Types supported
        """
        self._database.update_supported_managed_entities(self.device_id,
                                                         msg[SUPPORTED_MESSAGE_ENTITY_KEY])
        self._database.update_supported_message_types(self.device_id,
                                                      msg[SUPPORTED_MESSAGE_TYPES_KEY])

    def _status_to_text(self, success_code):
        return {
                RC.Success: "Success",
                RC.ProcessingError: "Processing Error",
                RC.NotSupported: "Not Supported",
                RC.ParameterError: "Paremeter Error",
                RC.UnknownEntity: "Unknown Entity",
                RC.UnknownInstance: "Unknown Instance",
                RC.DeviceBusy: "Device Busy",
                RC.InstanceExists: "Instance Exists"
            }.get(success_code, 'Unknown status code: {}'.format(success_code))

    def query_mib(self, class_id=None, instance_id=None, attributes=None):
        """
        Get MIB database information.

        This method can be used to request information from the database to the detailed
        level requested

        :param class_id:  (int) Managed Entity class ID
        :param instance_id: (int) Managed Entity instance
        :param attributes: (list or str) Managed Entity instance's attributes

        :return: (dict) The value(s) requested. If class/inst/attribute is
                        not found, an empty dictionary is returned
        :raises DatabaseStateError: If the database is not enabled or does not exist
        """
        from voltha.extensions.omci.database.mib_db_api import DatabaseStateError

        self.log.debug('query', class_id=class_id,
                       instance_id=instance_id, attributes=attributes)
        if self._database is None:
            raise DatabaseStateError('Database does not yet exist')

        return self._database.query(self._device_id, class_id=class_id,
                                    instance_id=instance_id,
                                    attributes=attributes)
