# This file is part of Scapy
# See http://www.secdev.org/projects/scapy for more information
# Copyright (C) Haggai Eran <haggai.eran@gmail.com>
# This program is published under a GPLv2 license

# scapy.contrib.description = RoCE v2
# scapy.contrib.status = loads

"""
RoCE: RDMA over Converged Ethernet
"""

from scapy.packet import Packet, bind_layers, Raw
from scapy.fields import BitEnumField, ByteEnumField, ByteField, XByteField, \
    ShortField, XShortField, XIntField, XLongField, BitField, XBitField, FCSField
from scapy.layers.inet import IP, UDP
from scapy.layers.l2 import Ether
from scapy.compat import raw
from scapy.error import warning
from zlib import crc32
import struct
from scapy.compat import Tuple

_transports = {
    'RC': 0x00,
    'UC': 0x20,
    'RD': 0x40,
    'UD': 0x60,
    'CNP': 0x80,
    'XRC': 0xA0,
}

_ops = {
    'SEND_FIRST': 0x00,
    'SEND_MIDDLE': 0x01,
    'SEND_LAST': 0x02,
    'SEND_LAST_WITH_IMMEDIATE': 0x03,
    'SEND_ONLY': 0x04,
    'SEND_ONLY_WITH_IMMEDIATE': 0x05,
    'RDMA_WRITE_FIRST': 0x06,
    'RDMA_WRITE_MIDDLE': 0x07,
    'RDMA_WRITE_LAST': 0x08,
    'RDMA_WRITE_LAST_WITH_IMMEDIATE': 0x09,
    'RDMA_WRITE_ONLY': 0x0a,
    'RDMA_WRITE_ONLY_WITH_IMMEDIATE': 0x0b,
    'RDMA_READ_REQUEST': 0x0c,
    'RDMA_READ_RESPONSE_FIRST': 0x0d,
    'RDMA_READ_RESPONSE_MIDDLE': 0x0e,
    'RDMA_READ_RESPONSE_LAST': 0x0f,
    'RDMA_READ_RESPONSE_ONLY': 0x10,
    'ACKNOWLEDGE': 0x11,
    'ATOMIC_ACKNOWLEDGE': 0x12,
    'COMPARE_SWAP': 0x13,
    'FETCH_ADD': 0x14,
    'RESYNC': 0x15,
    'SEND_LAST_WITH_INVALIDATE': 0x16,
    'SEND_ONLY_WITH_INVALIDATE': 0x17,
}


CNP_OPCODE = 0x81


def opcode(transport, op):
    # type: (str, str) -> Tuple[int, str]
    return (_transports[transport] + _ops[op], '{}_{}'.format(transport, op))


_bth_opcodes = dict([
    opcode('RC', 'SEND_FIRST'),
    opcode('RC', 'SEND_MIDDLE'),
    opcode('RC', 'SEND_LAST'),
    opcode('RC', 'SEND_LAST_WITH_IMMEDIATE'),
    opcode('RC', 'SEND_ONLY'),
    opcode('RC', 'SEND_ONLY_WITH_IMMEDIATE'),
    opcode('RC', 'RDMA_WRITE_FIRST'),
    opcode('RC', 'RDMA_WRITE_MIDDLE'),
    opcode('RC', 'RDMA_WRITE_LAST'),
    opcode('RC', 'RDMA_WRITE_LAST_WITH_IMMEDIATE'),
    opcode('RC', 'RDMA_WRITE_ONLY'),
    opcode('RC', 'RDMA_WRITE_ONLY_WITH_IMMEDIATE'),
    opcode('RC', 'RDMA_READ_REQUEST'),
    opcode('RC', 'RDMA_READ_RESPONSE_FIRST'),
    opcode('RC', 'RDMA_READ_RESPONSE_MIDDLE'),
    opcode('RC', 'RDMA_READ_RESPONSE_LAST'),
    opcode('RC', 'RDMA_READ_RESPONSE_ONLY'),
    opcode('RC', 'ACKNOWLEDGE'),
    opcode('RC', 'ATOMIC_ACKNOWLEDGE'),
    opcode('RC', 'COMPARE_SWAP'),
    opcode('RC', 'FETCH_ADD'),
    opcode('RC', 'SEND_LAST_WITH_INVALIDATE'),
    opcode('RC', 'SEND_ONLY_WITH_INVALIDATE'),

    opcode('UC', 'SEND_FIRST'),
    opcode('UC', 'SEND_MIDDLE'),
    opcode('UC', 'SEND_LAST'),
    opcode('UC', 'SEND_LAST_WITH_IMMEDIATE'),
    opcode('UC', 'SEND_ONLY'),
    opcode('UC', 'SEND_ONLY_WITH_IMMEDIATE'),
    opcode('UC', 'RDMA_WRITE_FIRST'),
    opcode('UC', 'RDMA_WRITE_MIDDLE'),
    opcode('UC', 'RDMA_WRITE_LAST'),
    opcode('UC', 'RDMA_WRITE_LAST_WITH_IMMEDIATE'),
    opcode('UC', 'RDMA_WRITE_ONLY'),
    opcode('UC', 'RDMA_WRITE_ONLY_WITH_IMMEDIATE'),

    opcode('RD', 'SEND_FIRST'),
    opcode('RD', 'SEND_MIDDLE'),
    opcode('RD', 'SEND_LAST'),
    opcode('RD', 'SEND_LAST_WITH_IMMEDIATE'),
    opcode('RD', 'SEND_ONLY'),
    opcode('RD', 'SEND_ONLY_WITH_IMMEDIATE'),
    opcode('RD', 'RDMA_WRITE_FIRST'),
    opcode('RD', 'RDMA_WRITE_MIDDLE'),
    opcode('RD', 'RDMA_WRITE_LAST'),
    opcode('RD', 'RDMA_WRITE_LAST_WITH_IMMEDIATE'),
    opcode('RD', 'RDMA_WRITE_ONLY'),
    opcode('RD', 'RDMA_WRITE_ONLY_WITH_IMMEDIATE'),
    opcode('RD', 'RDMA_READ_REQUEST'),
    opcode('RD', 'RDMA_READ_RESPONSE_FIRST'),
    opcode('RD', 'RDMA_READ_RESPONSE_MIDDLE'),
    opcode('RD', 'RDMA_READ_RESPONSE_LAST'),
    opcode('RD', 'RDMA_READ_RESPONSE_ONLY'),
    opcode('RD', 'ACKNOWLEDGE'),
    opcode('RD', 'ATOMIC_ACKNOWLEDGE'),
    opcode('RD', 'COMPARE_SWAP'),
    opcode('RD', 'FETCH_ADD'),
    opcode('RD', 'RESYNC'),

    opcode('UD', 'SEND_ONLY'),
    opcode('UD', 'SEND_ONLY_WITH_IMMEDIATE'),

    opcode('XRC', 'SEND_FIRST'),
    opcode('XRC', 'SEND_MIDDLE'),
    opcode('XRC', 'SEND_LAST'),
    opcode('XRC', 'SEND_LAST_WITH_IMMEDIATE'),
    opcode('XRC', 'SEND_ONLY'),
    opcode('XRC', 'SEND_ONLY_WITH_IMMEDIATE'),
    opcode('XRC', 'RDMA_WRITE_FIRST'),
    opcode('XRC', 'RDMA_WRITE_MIDDLE'),
    opcode('XRC', 'RDMA_WRITE_LAST'),
    opcode('XRC', 'RDMA_WRITE_LAST_WITH_IMMEDIATE'),
    opcode('XRC', 'RDMA_WRITE_ONLY'),
    opcode('XRC', 'RDMA_WRITE_ONLY_WITH_IMMEDIATE'),
    opcode('XRC', 'RDMA_READ_REQUEST'),
    opcode('XRC', 'RDMA_READ_RESPONSE_FIRST'),
    opcode('XRC', 'RDMA_READ_RESPONSE_MIDDLE'),
    opcode('XRC', 'RDMA_READ_RESPONSE_LAST'),
    opcode('XRC', 'RDMA_READ_RESPONSE_ONLY'),
    opcode('XRC', 'ACKNOWLEDGE'),
    opcode('XRC', 'ATOMIC_ACKNOWLEDGE'),
    opcode('XRC', 'COMPARE_SWAP'),
    opcode('XRC', 'FETCH_ADD'),
    opcode('XRC', 'SEND_LAST_WITH_INVALIDATE'),
    opcode('XRC', 'SEND_ONLY_WITH_INVALIDATE'),

    (CNP_OPCODE, 'CNP'),
])

class BTH(Packet):
    name = "BTH"
    fields_desc = [
        ByteEnumField("opcode", 0, _bth_opcodes),
        BitField("solicited", 0, 1),
        BitField("migreq", 0, 1),
        BitField("padcount", 0, 2),
        BitField("version", 0, 4),
        XShortField("pkey", 0xffff),
        BitField("fecn", 0, 1),
        BitField("becn", 0, 1),
        BitField("resv6", 0, 6),
        BitField("dqpn", 0, 24),
        BitField("ackreq", 0, 1),
        BitField("resv7", 0, 7),
        BitField("psn", 0, 24),

        FCSField("icrc", None, fmt="!I")
    ]

    @staticmethod
    def pack_icrc(icrc):
        # type: (int) -> bytes
        return struct.pack("!I", icrc & 0xffffffff)[::-1]

    def compute_icrc(self, p):
        # type: (bytes) -> bytes
        udp = self.underlayer
        if udp is None or not isinstance(udp, UDP):
            warning("Expecting UDP underlayer to compute checksum. Got %s.",
                    udp and udp.name)
            return self.pack_icrc(0)
        ip = udp.underlayer
        if isinstance(ip, IP):
            # pseudo-LRH / IP / UDP / BTH / payload
            pshdr = Raw(b'\xff' * 8) / ip.copy()
            pshdr.chksum = 0xffff
            pshdr.ttl = 0xff
            pshdr.tos = 0xff
            pshdr[UDP].chksum = 0xffff
            pshdr[BTH].fecn = 1
            pshdr[BTH].becn = 1
            pshdr[BTH].resv6 = 0xff
            bth = pshdr[BTH].self_build()
            payload = raw(pshdr[BTH].payload)
            # add ICRC placeholder just to get the right IP.totlen and
            # UDP.length
            icrc_placeholder = b'\xff\xff\xff\xff'
            pshdr[UDP].payload = Raw(bth + payload + icrc_placeholder)
            icrc = crc32(raw(pshdr)[:-4]) & 0xffffffff
            return self.pack_icrc(icrc)
        else:
            # TODO support IPv6
            warning("The underlayer protocol %s is not supported.",
                    ip and ip.name)
            return self.pack_icrc(0)

    # RoCE packets end with ICRC - a 32-bit CRC of the packet payload and
    # pseudo-header. Add the ICRC header if it is missing and calculate its
    # value.
    def post_build(self, p, pay):
        # type: (bytes, bytes) -> bytes
        p += pay
        if self.icrc is None:
            p = p[:-4] + self.compute_icrc(p)
        return p


class CNPPadding(Packet):
    name = "CNPPadding"
    fields_desc = [
        XLongField("reserved1", 0),
        XLongField("reserved2", 0),
    ]


def cnp(dqpn):
    # type: (int) -> BTH
    return BTH(opcode=CNP_OPCODE, becn=1, dqpn=dqpn) / CNPPadding()


class GRH(Packet):
    name = "GRH"
    fields_desc = [
        BitField("ipver", 6, 4),
        BitField("tclass", 0, 8),
        BitField("flowlabel", 6, 20),
        ShortField("paylen", 0),
        ByteField("nexthdr", 0),
        ByteField("hoplmt", 0),
        XBitField("sgid", 0, 128),
        XBitField("dgid", 0, 128),
    ]

class AETH(Packet):
    name = "AETH"
    fields_desc = [
        BitField("rsvd", 0, 1),
        BitEnumField("code", "RSVD", 2, {0: "ACK", 1: "RNR", 2: "RSVD", 3: "NAK"}),
        BitField("value", 0, 5),
        #XByteField("syndrome", 0),
        XBitField("msn", 0, 24),
    ]

class RETH(Packet):
    name = "RETH"
    fields_desc = [
        XLongField("va", 0),
        XIntField("rkey", 0),
        XIntField("dlen", 0),
    ]

class AtomicETH(Packet):
    name = "AtomicETH"
    fields_desc = [
        XLongField("va", 0),
        XIntField("rkey", 0),
        XLongField("comp", 0),
        XLongField("swap", 0),
    ]

class AtomicAckETH(Packet):
    name = "AtomicAckETH"
    fields_desc = [
        XLongField("orig", 0),
    ]

class ImmDt(Packet):
    name = "ImmDt"
    fields_desc = [
        XIntField("data", 0),
    ]

class IETH(Packet):
    name = "IETH"
    fields_desc = [
        XIntField("rkey", 0),
    ]

class RETHImmDt(Packet): # for RDMA_WRITE_ONLY_WITH_IMMEDIATE only
    name = "RETHImmdt"
    fields_desc = [
        XLongField("va", 0),
        XIntField("rkey", 0),
        XIntField("dlen", 0),
        XIntField("data", 0),
    ]

bind_layers(BTH, CNPPadding, opcode=CNP_OPCODE)

bind_layers(Ether, GRH, type=0x8915)
bind_layers(GRH, BTH)

#bind_layers(AETH, AtomicAckETH) this layer binding is not work
bind_layers(BTH, AETH, opcode=opcode('RC', 'ACKNOWLEDGE')[0])
bind_layers(BTH, AETH, opcode=opcode('RC', 'ATOMIC_ACKNOWLEDGE')[0])
bind_layers(BTH, AETH, opcode=opcode('RC', 'RDMA_READ_RESPONSE_FIRST')[0])
bind_layers(BTH, AETH, opcode=opcode('RC', 'RDMA_READ_RESPONSE_LAST')[0])
bind_layers(BTH, AETH, opcode=opcode('RC', 'RDMA_READ_RESPONSE_ONLY')[0])
bind_layers(BTH, AtomicETH, opcode=opcode('RC', 'COMPARE_SWAP')[0])
bind_layers(BTH, AtomicETH, opcode=opcode('RC', 'FETCH_ADD')[0])
bind_layers(BTH, IETH, opcode=opcode('RC', 'SEND_LAST_WITH_INVALIDATE')[0])
bind_layers(BTH, IETH, opcode=opcode('RC', 'SEND_ONLY_WITH_INVALIDATE')[0])
bind_layers(BTH, ImmDt, opcode=opcode('RC', 'SEND_LAST_WITH_IMMEDIATE')[0])
bind_layers(BTH, ImmDt, opcode=opcode('RC', 'SEND_ONLY_WITH_IMMEDIATE')[0])
bind_layers(BTH, ImmDt, opcode=opcode('RC', 'RDMA_WRITE_LAST_WITH_IMMEDIATE')[0])
bind_layers(BTH, RETH, opcode=opcode('RC', 'RDMA_READ_REQUEST')[0])
bind_layers(BTH, RETH, opcode=opcode('RC', 'RDMA_WRITE_FIRST')[0])
bind_layers(BTH, RETH, opcode=opcode('RC', 'RDMA_WRITE_ONLY')[0])
#bind_layers(BTH, RETH, opcode=opcode('RC', 'RDMA_WRITE_ONLY_WITH_IMMEDIATE')[0]) this layer binding is not work
bind_layers(BTH, RETHImmDt, opcode=opcode('RC', 'RDMA_WRITE_ONLY_WITH_IMMEDIATE')[0])
bind_layers(UDP, BTH, dport=4791)
bind_layers(UDP, BTH, sport=4791)
