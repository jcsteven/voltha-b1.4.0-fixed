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

import structlog
from twisted.internet.defer import  inlineCallbacks, returnValue, succeed

from voltha.adapters.adtran_olt.xpon.tcont import TCont
from voltha.adapters.adtran_olt.xpon.traffic_descriptor import TrafficDescriptor
from voltha.extensions.omci.omci_me import TcontFrame


class OnuTCont(TCont):
    """
    Adtran ONU specific implementation
    """
    def __init__(self, handler, alloc_id, traffic_descriptor,
                 name=None, vont_ani=None, is_mock=False):
        super(OnuTCont, self).__init__(alloc_id, traffic_descriptor,
                                       name=name, vont_ani=vont_ani)
        self._handler = handler
        self._is_mock = is_mock
        self._entity_id = None
        self.log = structlog.get_logger(device_id=handler.device_id, alloc_id=alloc_id)

    @property
    def entity_id(self):
        return self._entity_id

    @staticmethod
    def create(handler, tcont, td, is_mock=False):
        assert isinstance(tcont, dict), 'TCONT should be a dictionary'
        assert isinstance(td, TrafficDescriptor), 'Invalid Traffic Descriptor data type'

        return OnuTCont(handler,
                        tcont['alloc-id'],
                        td,
                        name=tcont['name'],
                        vont_ani=tcont['vont-ani'],
                        is_mock=is_mock)

    @inlineCallbacks
    def add_to_hardware(self, omci, tcont_entity_id):
        self.log.debug('add-to-hardware', tcont_entity_id=tcont_entity_id)

        self._entity_id = tcont_entity_id
        if self._is_mock:
            returnValue('mock')

        try:
            frame = TcontFrame(self.entity_id, self.alloc_id).set()
            results = yield omci.send(frame)

            status = results.fields['omci_message'].fields['success_code']
            failed_attributes_mask = results.fields['omci_message'].fields['failed_attributes_mask']
            unsupported_attributes_mask = results.fields['omci_message'].fields['unsupported_attributes_mask']
            self.log.debug('set-tcont', status=status,
                           failed_attributes_mask=failed_attributes_mask,
                           unsupported_attributes_mask=unsupported_attributes_mask)

        except Exception as e:
            self.log.exception('tcont-set', e=e)
            raise

        returnValue(results)

    @inlineCallbacks
    def remove_from_hardware(self, omci):
        self.log.debug('remove-from-hardware', tcont_entity_id=self.entity_id)
        if self._is_mock:
            returnValue('mock')

        # Release tcont by setting alloc_id=0xFFFF
        try:
            frame = TcontFrame(self.entity_id, 0xFFFF).set()
            results = yield omci.send(frame)

            status = results.fields['omci_message'].fields['success_code']
            self.log.debug('delete-tcont', status=status)

        except Exception as e:
            self.log.exception('tcont-delete', e=e)
            raise

        returnValue(results)
