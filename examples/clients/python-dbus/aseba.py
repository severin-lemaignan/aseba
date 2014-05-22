#!/usr/bin/python
# -*- coding: utf-8 -*-

"""
A Python binding for Aseba that relies on DBus for IPC.

Supports event sending/receiving as well as setting/getting an Aseba variable.

Typical usage:

    def my_callback(evt):
        pass

    with Aseba as aseba:
        aseba.on_event("evt_name", my_callback)
        aseba.send_event("evt_name or evt_id", [arg1, arg2, ...])

        aseba.set("node_name", "var_name", value)
        value = aseba.get("node_name", "var_name")


Aseba.event_freq is a dictionary that store the frequency at which each
(subscribed) event is received. For events that are emitted at fixed period, it
may be a convenient way to make sure the connection is in good state.

"""

__author__ = "Séverin Lemaignan, Stéphane Magnenat"

import dbus
import time

import xml.etree.ElementTree as ET

from dbus.mainloop.glib import DBusGMainLoop
import gobject
# required to prevent the glib main loop to interfere with Python threads
gobject.threads_init()
dbus.mainloop.glib.threads_init()

class AsebaException(Exception):
    def __init__(self, value):
        self.value = value
    def __str__(self):
        return repr(self.value)

class Aseba(object):

    NB_EVTS_FREQ = 10 # nb of events to be received before computing frequency

    def __init__(self, system_bus = False, dummy = False):
        self.dummy = dummy

        self.callbacks = {}
        self._events_last_evt = {}
        self._events_periods = {}
        self.events_freq = {}

        self.eventnames = []

        if not dummy:
            DBusGMainLoop(set_as_default=True)
            # init 
            if system_bus:
                self.bus = dbus.SystemBus()
            else:
                self.bus = dbus.SessionBus()

            try:
                self.network = dbus.Interface(
                        self.bus.get_object('ch.epfl.mobots.Aseba', '/'), 
                        dbus_interface='ch.epfl.mobots.AsebaNetwork')
            except dbus.exceptions.DBusException:
                raise AsebaException("Can not connect to Aseba DBus services! "
                                     "Is asebamedulla running?")


            # Configure event management
            eventfilter = self.network.CreateEventFilter()
            self.events = dbus.Interface(
                        self.bus.get_object('ch.epfl.mobots.Aseba', eventfilter), 
                        dbus_interface='ch.epfl.mobots.EventFilter')
            self.dispatch_handler = self.events.connect_to_signal('Event', self._dispatch_events)

    def clear_events(self):
        """ Use DBus introspection to get the list of event filters, and remove them.
        """
        try:
            introspect = dbus.Interface(
                        self.bus.get_object('ch.epfl.mobots.Aseba', "/events_filters"), 
                        dbus_interface=dbus.INTROSPECTABLE_IFACE)
            interface = introspect.Introspect()
        except dbus.exceptions.DBusException:
            # /events_filters not yet created -> no events to delete
            return

        root = ET.fromstring(interface)
        for n in root.iter("node"):
            if 'name' in n.attrib:
                evtfilter = dbus.Interface(
                        self.bus.get_object('ch.epfl.mobots.Aseba', "/events_filters/%s" % n.attrib['name']), 
                        dbus_interface='ch.epfl.mobots.EventFilter')
                evtfilter.Free()

    def __enter__(self):
        self.run()
        return self

    def __exit__(self, type, value, traceback):
        self.close()

    def run(self):
        # run event loop
        self.loop = gobject.MainLoop()
        self.loop.run()

    def close(self):
        self.loop.quit()
        if not self.dummy:
            self.dispatch_handler.remove()
            self.events.Free()

    def dbus_reply(self):
        # correct replay on D-Bus, ignore
        pass

    def dbus_error(self, e):
        assert(not self.dummy)

        # there was an error on D-Bus, stop loop
        self.close()
        raise Exception('dbus error: %s' % str(e))

    def get_nodes_list(self):
        if self.dummy: return []

        dbus_array = self.network.GetNodesList()
        size = len(dbus_array)
        return [str(dbus_array[x]) for x in range(0,size)]

    def set(self, node, var, value):
        if self.dummy: return
        self.network.SetVariable(node, var, value)

    def get(self, node, var):
        if self.dummy: return [0] * 10
        dbus_array = self.network.GetVariable(node, var)
        size = len(dbus_array)
        if (size == 1):
            return int(dbus_array[0])
        else:
            return [int(dbus_array[x]) for x in range(0,size)]

    def send_event(self, event_id, event_args):
        if self.dummy: return

        if isinstance(event_id, basestring):
            event_id = self._get_event_id(event_id)

        # events are sent asynchronously
        self.network.SendEvent(event_id, 
                               event_args,
                               reply_handler=self.dbus_reply,
                               error_handler=self.dbus_error)


    def load_events_list(self, path):
        tree = ET.parse(path)
        root = tree.getroot()
        self.eventnames = [node.attrib['name'] for node in root if node.tag == "event"]

    def get_event_frequency(self, event_id):

        if isinstance(event_id, basestring):
            event_id = self._get_event_id(event_id)

        if event_id not in self.events_freq:
            return 0

        return self.events_freq[event_id] 


    def _get_event_id(self, event_name):
        if event_name in self.eventnames:
            return self.eventnames.index(event_name)
        raise AsebaException("Event %s is unknown. Did you load the list" \
                             " of events with load_events_list?")

    def load_scripts(self, path):
        """ Loads a given Aseba script (aesl) on the Aseba network.
        """
        if self.dummy: return
        self.network.LoadScripts(path)

    def _dispatch_events(self, *args):
        id, name, vals = args

        if id in self.callbacks:

            self.callbacks[id](vals)

            # update frequency for this event
            now = time.time()
            self._events_periods[id].append(now - self._events_last_evt[id])
            self._events_last_evt[id] = now
            if len(self._events_periods[id]) == Aseba.NB_EVTS_FREQ:
                self.events_freq[id] = 1. / (sum(self._events_periods[id]) / float(Aseba.NB_EVTS_FREQ))
                self._events_periods[id] = []

    def on_event(self, event_id, cb):
        """
        Subscribe to an Aseba event

        :param event_id: the event name or the event numerical ID
        :param cb: the callback function that will be called with the content of the event as parameter
        """
        if self.dummy: return

        if isinstance(event_id, basestring):
            event_id = self._get_event_id(event_id)
 
        self.callbacks[event_id] = cb
        self._events_last_evt[event_id] = time.time()
        self._events_periods[event_id] = []
        self.events_freq[event_id] = 0.

        self.events.ListenEvent(event_id)

# *** TEST ***
if __name__ == '__main__':
    from optparse import OptionParser
    parser = OptionParser()
    parser.add_option("-s", "--system", action="store_true", dest="system", default=False,
            help="use the system bus instead of the session bus")

    (options, args) = parser.parse_args()

    def my_callback(evt):
        print(evt)

    with Aseba(options.system) as aseba:

        # 'timer0' is the name of the Aseba event you want to subscribe to.
        aseba.on_event("timer0", my_callback)

        print "List of nodes: ", aseba.get_nodes_list()

        aseba.set("thymio-II", "event.source", [1])
        aseba.send_event(0, [])

        print "Get variable temperature: ", aseba.get("thymio-II", "temperature")
        print "Get array acc: ", aseba.get("thymio-II", "acc")

