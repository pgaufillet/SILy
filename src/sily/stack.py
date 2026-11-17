"""Assembly of the mesh node of a board, on injected hardware.

Shared by the application (``sily.app``) and the bench console
(``sily.bench``), so that the bench measures the stack the boxes run. The
chain: radio settings checked against the regional profile, SX1262 adapter
(from the injected factory) started, bound to the mesh radio contract
(``sily.radio.MeshRadio``), neighbour filter, drop rates (``sily.drop``,
when set), link engine on the platform objects (``mesh.link.open_link``),
mesh routing (``mesh.routing.Routing``, or static routes: every
destination a neighbour), asyncio runner. The
hardware comes in as arguments (adapter factory, storage, retained memory,
random source, MAC address: a bundle such as ``sily.hw``), so that the
assembly runs on the unix port with fakes.
"""

import gc

import sily
from mesh import profile as profiles
from mesh.dodag import DodagInfo, default_info
from mesh.link import destination, open_link
from mesh.radio import NeighbourFilter
from mesh.reports import NodeInfo
from mesh.routing import Routing
from mesh.runner import Runner
from sily.drop import Drop
from sily.radio import MeshRadio, link_source, lora_config


class AllowAll:
    """Allowed set of a disabled neighbour filter."""

    def __contains__(self, item):
        return True


def firmware_version():
    """Return the application version as the 16-bit node information field (major, minor)."""
    major, minor = sily.__version__.split(".")[:2]
    return int(major) << 8 | int(minor)


class Stack:
    """The mesh node of one board.

    hw: the hardware bundle (``sily.hw``): ``adapter(lora_cfg)`` returns the
    radio adapter (``board.sx1262.Sx1262`` API: ``start``, ``run``,
    ``transmit``, ``busy``, the callbacks, ``modem``), ``storage(namespace)``,
    ``retained()``, ``random()`` the ``mesh.platform`` objects (frame
    counter in the NVS namespace ``mesh``), ``mac()`` the 6-byte MAC
    address of the node information. address, key (16 B frame key), sf;
    name: node information. profile: regional profile identifier; power (dBm), gain
    (antenna, dBi); allow: the addresses heard (None: every one); drop:
    address -> percent of its frames dropped (``sily.drop``, after the
    neighbour filter; None or empty: none); routing: the mesh routing
    (False: static routes); presence_s: presence period of the DODAG
    information (root only; topology period 6 times longer);
    extension, on_report: as ``mesh.routing.Routing``; wrap(stack, radio):
    returns a radio wrapper placed between the neighbour filter (or the
    drop wrapper) and the runner; clock: the runner's.

    Raises ValueError for radio settings outside the profile, before the
    adapter is made. Attributes: ``adapter``, ``filter``, ``drop`` (the
    `sily.drop.Drop`, None without drop rates), ``engine``, ``routing``
    (None with static routes), ``runner``, ``profile`` (the object),
    ``sf``; ``heap_radio`` and ``heap_node``: free heap after a collection
    once the radio is bound, and once the node is complete.
    """

    def __init__(
        self,
        hw,
        address,
        key,
        sf,
        name="",
        profile=profiles.EU_869,
        power=22,
        gain=0,
        allow=None,
        drop=None,
        routing=True,
        presence_s=None,
        extension=None,
        on_report=None,
        wrap=None,
        clock=None,
    ):
        regional = profiles.get(profile)
        if regional is None:
            raise ValueError("unknown profile %r" % (profile,))
        problems = profiles.validate(regional, sf, power, gain)
        if problems:
            raise ValueError("bad radio settings: %s" % problems)
        self.profile = regional
        self.sf = sf
        self.adapter = hw.adapter(lora_config(regional, sf, power))
        self.adapter.start()
        self.filter = NeighbourFilter(MeshRadio(self.adapter), AllowAll(), link_source)
        if allow is not None:
            self.filter.allowed = set(allow)
        gc.collect()
        self.heap_radio = gc.mem_free()
        self.engine = open_link(
            address,
            key,
            hw.storage("mesh"),
            hw.retained(),
            hw.random(),
            regional,
            sf,
            next_hop=None if routing else destination,  # static: every one a neighbour
        )
        self.routing = None
        if routing:
            info = default_info(sf, profile)
            if presence_s is not None:
                info = DodagInfo(
                    info.flags,
                    presence_s,
                    6 * presence_s,
                    info.imin_slots,
                    info.max_increase,
                    info.profile,
                )
            node = NodeInfo(hw.mac(), firmware_version(), name)
            # The root's version: the one after the network's (no persistent state).
            self.routing = Routing(
                self.engine, profile, info, version=None, node_info=node, extension=extension
            )
            self.routing.on_report = on_report
        radio = self.filter
        self.drop = None
        if drop:
            radio = self.drop = Drop(radio, drop, link_source)
        if wrap is not None:
            radio = wrap(self, radio)
        self.runner = Runner(self.engine, radio, clock)
        gc.collect()
        self.heap_node = gc.mem_free()

    def close(self):
        """Stop the runner and put the radio in standby: the node no longer receives."""
        self.runner.stop()
        self.adapter.modem.standby()
