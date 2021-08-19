import logging
import random
import socket
import struct
import sys

# from logging import debug, info, warning, error, critical
from copy import deepcopy
from roce_enum import *
from scapy.all import *
from roce import *

ROCE_PORT = 4791
UDP_BUF_SIZE = 1024
PKEY_DEFAULT = 0xFFFF
CREDIT_CNT_INVALID = 31
ATOMIC_BYTE_SIZE = 8

MAX_SSN = 2**24
MAX_MSN = 2**24
MAX_PSN = 2**24

class Util:
    def check_pkt_size(mtu, pkt):
        op = pkt[BTH].opcode
        # TODO: handle invalid request error: Length errors / Responder Class C
        if RC.first_req_pkt(op) or op == RC.RDMA_READ_RESPONSE_FIRST:
            #print(f'len(pkt[Raw].load)={len(pkt[Raw].load)}')
            assert len(pkt[Raw].load) == mtu
        elif RC.mid_req_pkt(op) or op == RC.RDMA_READ_RESPONSE_MIDDLE:
            #print(f'len(pkt[Raw].load)={len(pkt[Raw].load)}')
            assert len(pkt[Raw].load) == mtu
        elif RC.last_req_pkt(op) or op == RC.RDMA_READ_RESPONSE_LAST:
            #print(f'len(pkt[Raw].load)={len(pkt[Raw].load)}')
            assert len(pkt[Raw].load) <= mtu and len(pkt[Raw].load) > 0
        elif RC.only_req_pkt(op) or op == RC.RDMA_READ_RESPONSE_ONLY:
            if pkt.haslayer(Raw):
                #print(f'len(pkt[Raw].load)={len(pkt[Raw].load)}')
                assert len(pkt[Raw].load) <= mtu
        return True

    def check_pre_cur_ops(pre_op, cur_op):
        # TODO: handle invalid request error: Out of Sequence OpCode / Responder Class C
        if pre_op == RC.SEND_FIRST or pre_op == RC.SEND_MIDDLE:
            assert cur_op == RC.SEND_MIDDLE or RC.send_last(cur_op)
        elif pre_op == RC.RDMA_WRITE_FIRST or pre_op == RC.RDMA_WRITE_MIDDLE:
            assert cur_op == RC.RDMA_WRITE_MIDDLE or RC.write_last(cur_op)
        elif pre_op == RC.RDMA_READ_RESPONSE_FIRST or pre_op == RC.RDMA_READ_RESPONSE_MIDDLE:
            # Allow out of order ACK in between read response, or NAK to early terminate read response
            assert cur_op == RC.RDMA_READ_RESPONSE_MIDDLE or cur_op == RC.RDMA_READ_RESPONSE_LAST or cur_op == RC.ACKNOWLEDGE
        elif (RC.last_req_pkt(pre_op) or RC.only_req_pkt(pre_op) or RC.atomic(pre_op)
                or pre_op == RC.RDMA_READ_RESPONSE_LAST or pre_op == RC.RDMA_READ_RESPONSE_ONLY
                or pre_op == RC.ATOMIC_ACKNOWLEDGE or pre_op == RC.ACKNOWLEDGE):
            # Expect first/only request or first/only response or ack, not middle/last
            assert not (RC.mid_req_pkt(cur_op) or RC.last_req_pkt(cur_op)
                        or cur_op == RC.RDMA_READ_RESPONSE_MIDDLE or cur_op == RC.RDMA_READ_RESPONSE_LAST)
        return True

    def check_op_with_access_flags(rc_op, access_flags): # Check operation w.r.t. MR or QP flags
        if RC.send(rc_op):
            assert ACCESS_FLAGS.LOCAL_WRITE & access_flags, 'send op needs RQ/MR has local write permission'
        elif RC.write(rc_op):
            assert ACCESS_FLAGS.REMOTE_WRITE & access_flags, 'write op needs RQ/MR has remote write permission'
        elif rc_op == RC.RDMA_READ_REQUEST:
            assert ACCESS_FLAGS.REMOTE_READ & access_flags, 'read op needs RQ/MR has remote read permission'
        elif RC.atomic(rc_op):
            assert ACCESS_FLAGS.REMOTE_ATOMIC & access_flags, 'atomic op needs RQ/MR has remote atomic permission'
        elif RC.read_resp(rc_op):
            assert ACCESS_FLAGS.LOCAL_WRITE & access_flags, 'read response needs SQ/MR has local write permission'
        elif rc_op == RC.ATOMIC_ACKNOWLEDGE:
            assert ACCESS_FLAGS.LOCAL_WRITE & access_flags, 'atomic response needs SQ/MR has local write permission'
        return True

    def check_addr_aligned(addr, mr):
        addr_in_mr = addr
        if ACCESS_FLAGS.ZERO_BASED & mr.flags():
            addr_in_mr = mr.addr() + addr
        # TODO: handle remote access error: length exceeds MR size / Responder Class C
        assert addr_in_mr >= mr.addr() and addr_in_mr + ATOMIC_BYTE_SIZE <= mr.addr() + mr.len(), f'address={addr} is not within MR'
        # TODO: handle invalid request error: Misaligned ATOMIC / Responder Class C
        assert addr_in_mr == ((addr_in_mr >> 3) << 3)

        return True

    # PSN compare logic:
    # psn_a == psn_b: 0
    # psn_a > psn_b: 1
    # psn_a < psn_b: -1
    def psn_compare(psn_a, psn_b, cur_max_psn):
        assert cur_max_psn >= 0 and cur_max_psn < MAX_PSN, 'cur_max_psn is invalid'
        assert psn_a >= 0 and psn_a < MAX_PSN, 'psn_a is invalid'
        assert psn_b >= 0 and psn_b < MAX_PSN, 'psn_b is invalid'

        if psn_a == psn_b:
            return 0
        else:
            oldest_psn = (cur_max_psn - (MAX_PSN / 2)) % MAX_PSN
            if psn_a < psn_b:
                if oldest_psn <= psn_a:
                    return -1
                elif psn_b <= oldest_psn:
                    return -1
                else:
                    return 1
            else: # psn_a > psn_b
                if oldest_psn >= psn_a:
                    return 1
                elif psn_b >= oldest_psn:
                    return 1
                else:
                    return -1

    def previous_psn(cur_psn):
        return (cur_psn - 1) % MAX_PSN

    def next_psn(cur_psn):
        return (cur_psn + 1) % MAX_PSN

    def psn_range(start_psn, end_psn):
        cur_psn = start_psn
        while cur_psn != end_psn:
            yield cur_psn
            cur_psn = Util.next_psn(cur_psn)

class MR:
    def __init__(self, va, length, access_flags, lkey, rkey):
        #assert ACCESS_FLAGS.ZERO_BASED & access_flags, 'only zero-based address supported'
        self.va = va
        self.local_key = lkey
        self.remote_key = rkey
        self.length = length
        self.access_flags = access_flags
        self.byte_data = bytearray(struct.pack(f'<{self.len()}s', b''))
        # self.pos = 0

    def addr(self):
        return self.va

    def lkey(self):
        return self.local_key

    def rkey(self):
        return self.remote_key

    def len(self):
        return self.length

    def flags(self):
        return self.access_flags

    def write(self, byte_data, addr = 0):
        addr_in_mr = addr if ACCESS_FLAGS.ZERO_BASED & self.flags() else addr - self.addr()
        assert addr_in_mr >= 0 and addr_in_mr + len(byte_data) <= self.len(), 'write address and size not within MR'
        self.byte_data[addr_in_mr : (addr_in_mr + len(byte_data))] = byte_data
        # self.pos = addr_in_mr + len(byte_data)

    # def append(self, byte_data):
    #     assert self.pos + len(byte_data) <= self.len(), 'append size not within MR'
    #     self.byte_data[self.pos : (self.pos + len(byte_data))] = byte_data
    #     self.pos += len(byte_data)

    def read(self, addr, size):
        addr_in_mr = addr if ACCESS_FLAGS.ZERO_BASED & self.flags() else addr - self.addr()
        assert addr_in_mr >= 0 and addr_in_mr + size <= self.len(), 'read address and size not within MR'
        return self.byte_data[addr_in_mr: (addr_in_mr + size)]

    # def read_all(self):
    #     assert len(self.byte_data) == self.len()
    #     return self.byte_data

class PD:
    def __init__(self, pdn):
        self.pdn = pdn
        self.qp_dict = {}
        #self.cq_dict = {}
        self.mr_dict = {}
        self.next_key = 1
    
    def reg_mr(self, va, length, access_flags):
        mr = MR(va = va, length = length, access_flags = access_flags, lkey = self.next_key, rkey = self.next_key)
        self.mr_dict[mr.lkey()] = mr
        self.mr_dict[mr.rkey()] = mr
        self.next_key += 1
        return mr

    def dereg_mr(self, mr):
        del self.mr_dict[mr.rkey()]
        if mr.lkey() in self.mr_dict:
            del self.mr_dict[mr.lkey()]

    def has_mr(self, lrkey):
        return lrkey in self.mr_dict

    def get_mr(self, lrkey):
        return self.mr_dict[lrkey]

    def add_qp(self, qp):
        self.qp_dict[qp.qpn()] = qp

    def validate_mr(self, rc_op, lrkey, addr, data_size):
        assert self.has_mr(lrkey), 'invalid lkey or rkey'
        mr = self.get_mr(lrkey)
        addr_in_mr = addr
        if ACCESS_FLAGS.ZERO_BASED & mr.flags():
            addr_in_mr = mr.addr() + addr

        # TODO: handle invalid request error: Length error / Responder Class C
        assert addr_in_mr >= mr.addr() and addr_in_mr + data_size <= mr.addr() + mr.len(), 'address or length not within MR'
        # TODO: handle remote access error: R_Key Violation / Responder Class C
        assert Util.check_op_with_access_flags(rc_op, mr.flags()), 'no enough permission for the operation'

        return True

class CQE:
    def __init__(self, wr_id, status, opcode, length, qpn, src_qp, wc_flags, imm_data_or_inv_rkey = None):
        self.wr_id = wr_id
        self.cqe_status = status
        self.opcode = opcode
        self.length = length
        self.qpn = qpn
        self.src_qp = src_qp
        self.wc_flags = wc_flags
        self.imm_data_inv_rkey = imm_data_or_inv_rkey

    def id(self):
        return self.wr_id
        
    def op(self):
        return self.opcode
        
    def len(self):
        return self.length
        
    def local_qpn(self): # local QPN
        return self.qpn

    def sqpn(self):
        return self.src_qp

    def status(self):
        return self.cqe_status

    def imm_data_or_inv_rkey(self):
        return self.imm_data_inv_rkey

class CQ:
    def __init__(self, cqn):
        self.cqn = cqn
        self.cq = []

    def pop(self):
        return self.cq.pop(0)

    def push(self, cqe):
        self.cq.append(cqe)

    def empty(self):
        return not bool(self.cq)

# class SGE:
#     def __init__(self, addr, length, lkey, data = b''):
#         self.addr = addr
#         self.length = length
#         self.lkey = lkey
#         self.byte_data = data
    
#     def data(self):
#         return self.byte_data

#     def len(self):
#         return self.length

#     def write(self, byte_data):
#         self.byte_data = byte_data

#     def read(self):
#         return self.byte_data

#     def append(self, byte_data):
#         self.byte_data += byte_data

# class SGL:
#     def __init__(self):
#         self.sg_list = []
#         self.length = 0
#         self.byte_data = b''
    
#     def append(self, sge):
#         self.sg_list.append(sge)
#         self.length += sge.len()
#         self.byte_data += sge.data() # TODO: do not copy data

#     def data(self):
#         return self.byte_data

#     def len(self):
#         return self.length

class SG:
    def __init__(self, pos_in_mr, length, lkey):
        self.pos_in_mr = pos_in_mr
        self.length = length
        self.local_key = lkey

    def addr(self):
        return self.pos_in_mr

    def len(self):
        return self.length

    def lkey(self):
        return self.local_key

class SendWR:
    def __init__(self, opcode, sgl,
        wr_id = None,
        send_flags = DEFAULT_FLAG,
        rmt_va = None,
        rkey = None,
        compare_add = None,
        swap = None,
        imm_data_or_inv_rkey = None,
    ):
        self.opcode = opcode
        self.send_flags = send_flags
        self.sgl = sgl
        self.wr_id = wr_id
        self.rmt_va = rmt_va
        self.remote_key = rkey
        self.compare_add_data = compare_add
        self.swap_data = swap
        self.imm_data_inv_rkey = imm_data_or_inv_rkey

    def id(self):
        return self.wr_id

    def len(self):
        return self.sgl.len()

    def op(self):
        return self.opcode

    def lkey(self):
        # TODO: handle the case of sgl is None
        return self.sgl.lkey()

    def rkey(self):
        return self.remote_key

    def laddr(self):
        return self.sgl.addr()

    def raddr(self):
        return self.rmt_va

    def flags(self):
        return self.send_flags

    def imm_data_or_inv_rkey(self):
        return self.imm_data_inv_rkey

    def comp(self):
        return self.compare_add_data

    def swap(self):
        return self.swap_data

class RecvWR:
    def __init__(self, sgl, wr_id = 0):
        self.sgl = sgl
        self.wr_id = wr_id

    def id(self):
        return self.wr_id

    def lkey(self):
        return self.sgl.lkey()

    def addr(self):
        return self.sgl.addr()

class ReadRespCtx:
    def __init__(self, read_mr, read_dlen, read_laddr, read_offset, read_wr_id, read_ssn, orig_read_req_psn):
        self.read_mr = read_mr
        self.read_dlen = read_dlen
        self.read_laddr = read_laddr
        self.read_offset = read_offset
        self.read_wr_id = read_wr_id
        self.read_ssn = read_ssn
        self.orig_read_req_psn = orig_read_req_psn

class SQ:
    def __init__(self, pd, cq, qpn, sq_psn, pmtu, access_flags, use_ipv6,
        pkey = PKEY_DEFAULT,
        draining = 0,
        max_rd_atomic = 10,
        max_dest_rd_atomic = 10,
        min_rnr_timer = 10,
        timeout = 10,
        retry_cnt = 3,
        rnr_rery = 3,
    ):
        self.sq = []
        self.qps = QPS.INIT
        self.pd = pd # TODO: check pd match for each req
        self.cq = cq
        self.qpn = qpn
        self.sq_psn = sq_psn % MAX_PSN
        self.pmtu = pmtu
        self.ssn = 1
        self.draining = draining

        self.dgid = None
        self.dst_qpn = None
        self.access_flags = access_flags
        self.pkey = pkey
        self.draining = draining
        self.max_rd_atomic = max_rd_atomic
        self.max_dest_rd_atomic = max_dest_rd_atomic
        self.min_rnr_timer = min_rnr_timer
        self.timeout = timeout
        self.retry_cnt = retry_cnt
        self.rnr_rery = rnr_rery

        self.use_ipv6 = use_ipv6
        self.req_pkt_dict = {}
        self.send_wqe_dict = {}
        #self.oldest_psn = sq_psn
        self.min_unacked_psn = sq_psn

        self.cur_read_resp_ctx = None

    def modify(self,
        qps = None,
        pmtu = None,
        sq_psn = None,
        dgid = None,
        dst_qpn = None,
        access_flags = None,
        pkey = None,
        sq_draining = None,
        max_rd_atomic = None,
        max_dest_rd_atomic = None,
        min_rnr_timer = None,
        timeout = None,
        retry_cnt = None,
        rnr_rery = None,
    ):
        if qps:
            self.qps = qps
        if pmtu:
            self.pmtu = pmtu
        if sq_psn:
            self.sq_psn = sq_psn % MAX_PSN
        if dgid:
            self.dgid = dgid
        if dst_qpn:
            self.dst_qpn = dst_qpn # qpn in number instread of hex string
        if access_flags:
            self.access_flags = access_flags
        if pkey:
            self.pkey = pkey
        if sq_draining:
            self.sq_draining = sq_draining
        if max_rd_atomic:
            self.max_rd_atomic = max_rd_atomic
        if max_dest_rd_atomic:
            self.max_dest_rd_atomic = max_dest_rd_atomic
        if min_rnr_timer:
            self.min_rnr_timer = min_rnr_timer
        if timeout:
            self.timeout = timeout
        if retry_cnt:
            self.retry_cnt = retry_cnt
        if rnr_rery:
            self.rnr_rery = rnr_rery

    def push(self, wr):
        assert self.qps == QPS.RTS, 'QP state is not RTS'
        wr_op = wr.op()
        # TODO: handle immediate errors, unsupported opcode
        assert WR_OPCODE.send(wr_op) or WR_OPCODE.write(wr_op) or WR_OPCODE.atomic(wr_op) or wr_op == WR_OPCODE.RDMA_READ, 'send WR has unsupported opcode'
        # TODO: handle immediate errors
        if wr.op() in [WR_OPCODE.SEND_WITH_IMM, WR_OPCODE.SEND_WITH_INV, WR_OPCODE.RDMA_WRITE_WITH_IMM]:
            assert wr.imm_data_or_inv_rkey(), 'send/write with immediate data or send with invalidate requires send WR has imm_data_or_inv_rkey'
        if WR_OPCODE.atomic(wr.op()):
            assert wr.len() >= ATOMIC_BYTE_SIZE, 'atomic WR has no enough buffer length to receive atomic response'
        if wr.len() > 0:
            local_key = wr.lkey()
            # TODO: handle immediate error
            assert self.pd.has_mr(local_key), 'send WR has invalid lkey'
            mr = self.pd.get_mr(local_key)
            # TODO: handle immediate error
            assert wr.laddr() + wr.len() <= mr.len(), 'send WR local SG is not within its MR'
        
        self.sq.append(wr)

    def pop(self):
        sr = self.sq.pop(0)
        cssn = self.ssn
        self.send_wqe_dict[cssn] = sr
        self.ssn = (self.ssn + 1) % MAX_SSN
        return (sr, cssn)

    def empty(self):
        return not bool(self.sq)

    def sqpn(self):
        return self.qpn

    def dqpn(self):
        return self.dst_qpn

    def get_qp_access_flags(self):
        return self.access_flags

    def is_expected_resp(self, resp_psn):
        if self.min_unacked_psn == self.sq_psn:
            # No response expected
            return False
        else:
            assert Util.psn_compare(self.min_unacked_psn, self.sq_psn, self.sq_psn) < 0, 'min unacked PSN not < SQ PSN'
            if (Util.psn_compare(self.min_unacked_psn, resp_psn, self.sq_psn) <= 0
                and Util.psn_compare(self.sq_psn, resp_psn, self.sq_psn) > 0):
                return True
            else:
                # Either dup or illegal response
                return False

    def handle_dup_or_illegal_resp(self, resp):
        if self.min_unacked_psn == self.sq_psn: # No response expected
            logging.debug(f'SQ={self.sqpn()} received ghost response: ' + resp.show(dump = True))
        else: # SQ discard duplicate or illegal response, except for unsolicited flow control credit
            psn_comp_res = Util.psn_compare(resp[BTH].psn, self.min_unacked_psn, self.sq_psn)
            assert psn_comp_res != 0, 'should handle duplicate or illegal response'
            if psn_comp_res < 0: # Dup resp
                logging.debug(f'SQ={self.sqpn()} received duplicate response: ' + resp.show(dump = True))
                nxt_psn = Util.next_psn(resp[BTH].psn)
                if nxt_psn == self.min_unacked_psn: # Unsolicited flow control credit
                    assert AETH in resp, 'unsolicited flow control credit ACK should have AETH'
                    assert resp[AETH].code == 0, 'unsolicited flow control credit ACK code should be 0'
                    credit_cnt = resp[AETH].value
                    logging.debug(f'SQ={self.sqpn()} received unsolicited flow control credit={credit_cnt}')
            else: # Illegal response, just discard
                assert Util.psn_compare(self.sq_psn, resp[BTH].psn, self.sq_psn) <= 0, 'should handle illegal response'
                logging.debug(f'SQ={self.sqpn()} received illegal response: ' + resp.show(dump = True))

    def process_one(self):
        if not self.dqpn():
            raise Exception(f'SQ={self.sqpn()} has no destination QPN')
        elif not self.dgid:
            raise Exception(f'SQ={self.sqpn()} has no destination GID')

        sr, cssn = self.pop()
        if WR_OPCODE.send(sr.op()):
            self.process_send_req(sr, cssn)
        elif WR_OPCODE.write(sr.op()):
            self.process_write_req(sr, cssn)
        elif WR_OPCODE.RDMA_READ == sr.opcode:
            self.process_read_req(sr, cssn)
        elif WR_OPCODE.atomic(sr.op()):
            self.process_atomic_req(sr, cssn)
        else:
            raise Exception(f'SQ={self.sqpn()} met unsupported opcode: {sr.opcode}')

    def send_pkt(self, cssn, req, save_pkt = True):
        cpsn = req[BTH].psn
        if save_pkt:
            self.req_pkt_dict[cpsn] = (req, cssn)

        # ip_hex = socket.inet_aton('192.168.122.190').hex()
        # dst_ipv6 = socket.inet_ntop(socket.AF_INET6, bytes.fromhex(self.dgid))
        dst_ipv6 = socket.inet_ntop(socket.AF_INET6, self.dgid)
        dst_ipv4 = dst_ipv6.replace('::ffff:', '')
        dst_ip = dst_ipv6 if self.use_ipv6 else dst_ipv4
        logging.debug(f'dest IP={dst_ip}')

        pkt = IP(dst=dst_ip)/UDP(dport=ROCE_PORT, sport=self.sqpn())/req
        logging.debug(f'SQ={self.sqpn()} sent out request: ' + pkt.show(dump = True))
        send(pkt)

    def process_send_req(self, sr, cssn):
        assert WR_OPCODE.send(sr.op()), 'should be send operation'
        addr = sr.laddr()
        send_size = sr.len()
        send_data = b''
        if send_size:
            mr = self.pd.get_mr(sr.lkey())
            send_data = mr.read(addr = addr, size = send_size)

        send_req_pkt_num = math.ceil(sr.len() / self.pmtu) if send_size > 0 else 1
        cpsn = self.sq_psn
        dqpn = self.dqpn()
        ackreq = True if SEND_FLAGS.SIGNALED & sr.flags() else False
        solicited = True if SEND_FLAGS.SOLICITED & sr.flags() else False

        if send_req_pkt_num > 1:
            send_bth = BTH(
                opcode = RC.SEND_FIRST,
                psn = cpsn,
                dqpn = dqpn,
                ackreq = False,
                solicited = False,
            )
            send_req = send_bth/Raw(load = send_data[0 : self.pmtu])
            self.send_pkt(cssn, send_req)

            send_req_mid_pkt_num = send_req_pkt_num - 2
            for i in range(send_req_mid_pkt_num):
                send_bth = BTH(
                    opcode = RC.SEND_MIDDLE,
                    psn = cpsn + i + 1,
                    dqpn = dqpn,
                    ackreq = False,
                    solicited = False,
                )
                send_req = send_bth/Raw(load = send_data[((i + 1) * self.pmtu) : ((i + 2) * self.pmtu)])
                self.send_pkt(cssn, send_req)

        rc_op = None
        if send_req_pkt_num == 1:
            if sr.op() == WR_OPCODE.SEND_WITH_IMM:
                rc_op = RC.SEND_ONLY_WITH_IMMEDIATE
            elif sr.op() == WR_OPCODE.SEND_WITH_INV:
                rc_op = RC.SEND_ONLY_WITH_INVALIDATE
            else:
                rc_op = RC.SEND_ONLY
        else:
            if sr.op() == WR_OPCODE.SEND_WITH_IMM:
                rc_op = RC.SEND_LAST_WITH_IMMEDIATE
            elif sr.op() == WR_OPCODE.SEND_WITH_INV:
                rc_op = RC.SEND_LAST_WITH_INVALIDATE
            else:
                rc_op = RC.SEND_LAST
        send_bth = BTH(
            opcode = rc_op,
            psn = cpsn + send_req_pkt_num - 1,
            dqpn = dqpn,
            ackreq = ackreq,
            solicited = solicited,
        )
        send_req = None
        if RC.has_imm(rc_op):
            imm_data = ImmDt(data = sr.imm_data_or_inv_rkey())
            send_req = send_bth/imm_data
        elif RC.has_inv(rc_op):
            send_ieth = IETH(rkey = sr.imm_data_or_inv_rkey())
            send_req = send_bth/send_ieth
        else:
            send_req = send_bth
        if send_size > 0:
            raw_pkt = Raw(load = send_data[((send_req_pkt_num - 1) * self.pmtu) : send_size])
            send_req = send_req/raw_pkt
        self.send_pkt(cssn, send_req)
        self.sq_psn = (self.sq_psn + send_req_pkt_num) % MAX_PSN

    def process_write_req(self, sr, cssn):
        assert WR_OPCODE.write(sr.op()), 'should be write operation'
        addr = sr.laddr()
        write_size = sr.len()
        write_data = b''
        if write_size:
            mr = self.pd.get_mr(sr.lkey())
            write_data = mr.read(addr = addr, size = write_size)

        write_req_pkt_num = math.ceil(write_size / self.pmtu) if write_size else 1
        cpsn = self.sq_psn
        dqpn = self.dqpn()
        ackreq = True if SEND_FLAGS.SIGNALED & sr.flags() else False
        solicited = False

        write_reth = RETH(va = sr.raddr(), rkey = sr.rkey(), dlen = write_size)
        if write_req_pkt_num > 1:
            write_bth = BTH(
                opcode = RC.RDMA_WRITE_FIRST,
                psn = cpsn,
                dqpn = dqpn,
                ackreq = False,
                solicited = False,
            )
            write_req = write_bth/write_reth/Raw(load = write_data[0 : self.pmtu])
            self.send_pkt(cssn, write_req)

            write_req_mid_pkt_num = write_req_pkt_num - 2
            for i in range(write_req_mid_pkt_num):
                write_bth = BTH(
                    opcode = RC.RDMA_WRITE_MIDDLE,
                    psn = cpsn + i + 1,
                    dqpn = dqpn,
                    ackreq = False,
                    solicited = False,
                )
                write_req = write_bth/Raw(load = write_data[((i + 1) * self.pmtu) : ((i + 2) * self.pmtu)])
                self.send_pkt(cssn, write_req)

        rc_op = None
        solicited = False
        if write_req_pkt_num == 1:
            if sr.op() == WR_OPCODE.RDMA_WRITE_WITH_IMM:
                rc_op = RC.RDMA_WRITE_ONLY_WITH_IMMEDIATE
                solicited = True if SEND_FLAGS.SOLICITED & sr.flags() else False
            else:
                rc_op = RC.RDMA_WRITE_ONLY
        else:
            if sr.op() == WR_OPCODE.RDMA_WRITE_WITH_IMM:
                rc_op = RC.RDMA_WRITE_LAST_WITH_IMMEDIATE
                solicited = True if SEND_FLAGS.SOLICITED & sr.flags() else False
            else:
                rc_op = RC.RDMA_WRITE_LAST
        write_bth = BTH(
            opcode = rc_op,
            psn = cpsn + write_req_pkt_num - 1,
            dqpn = dqpn,
            ackreq = ackreq,
            solicited = solicited,
        )
        write_req = None
        if RC.only_req_pkt(rc_op):
            write_req = write_bth/write_reth
        else:
            write_req = write_bth
        if RC.has_imm(rc_op):
            #imm_data = ImmDt(data = sr.imm_data_or_inv_rkey())
            #write_req = write_req/imm_data
            reth_imm_data = RETHImmDt(va = sr.raddr(), rkey = sr.rkey(), dlen = write_size, data = sr.imm_data_or_inv_rkey())
            write_req = write_bth/reth_imm_data
        if write_size > 0:
            raw_pkt = Raw(load = write_data[((write_req_pkt_num - 1) * self.pmtu) : write_size])
            write_req = write_req/raw_pkt
        self.send_pkt(cssn, write_req)
        self.sq_psn = (self.sq_psn + write_req_pkt_num) % MAX_PSN

    def process_read_req(self, sr, cssn):
        assert sr.op() == WR_OPCODE.RDMA_READ, 'should be read operation'
        # TODO: locally detected error: Local Memory Protection / Requester Class B
        assert ACCESS_FLAGS.LOCAL_WRITE & self.get_qp_access_flags(), 'read op should have write permission to local MR'

        read_size = sr.len()
        read_resp_pkt_num = math.ceil(read_size / self.pmtu) if read_size > 0 else 1
        cpsn = self.sq_psn
        dqpn = self.dqpn()

        read_bth = BTH(
            opcode = RC.RDMA_READ_REQUEST,
            psn = cpsn,
            dqpn = dqpn,
            ackreq = True,
        )
        read_reth = RETH(va = sr.raddr(), rkey = sr.rkey(), dlen = read_size)
        read_req = read_bth/read_reth
        self.send_pkt(cssn, read_req)
        self.sq_psn = (self.sq_psn + read_resp_pkt_num) % MAX_PSN

    def process_atomic_req(self, sr, cssn):
        assert WR_OPCODE.atomic(sr.op()), 'should be atomic operation'
        # TODO: handle locally detected error: Local Memory Protection / Requester Class B
        assert ACCESS_FLAGS.LOCAL_WRITE & self.get_qp_access_flags(), 'atomic op should have write permission to local MR'

        rc_op = RC.COMPARE_SWAP if sr.op() == WR_OPCODE.ATOMIC_CMP_AND_SWP else RC.FETCH_ADD
        cpsn = self.sq_psn
        dqpn = self.dqpn()
        atomic_bth = BTH(
            opcode = rc_op,
            psn = cpsn,
            dqpn = dqpn,
            ackreq = True,
        )
        atomic_eth = AtomicETH(
            va = sr.raddr(),
            rkey = sr.rkey(),
            comp = sr.comp(),
            swap = sr.swap(),
        )
        atomic_req = atomic_bth/atomic_eth
        self.send_pkt(cssn, atomic_req)
        self.sq_psn = (self.sq_psn + 1) % MAX_PSN

    def handle_normal_resp(self, resp):
        assert resp[BTH].dqpn == self.qpn, 'QPN not match with ACK packet'
        assert self.is_expected_resp(resp[BTH].psn), 'should expect valid response, not duplicate or illegal one'
        col_ack_res, psn_begin_retry = self.coalesce_ack(resp[BTH].psn)
        self.min_unacked_psn = psn_begin_retry
        if not col_ack_res: # There are read or atomic requests being implicitly NAK'd, should retry
            logging.debug(f'SQ={self.sqpn()} has implicit ACK-ed packtes, needs to retry from PSN={psn_begin_retry}')
            self.min_unacked_psn = psn_begin_retry
            self.retry(psn_begin_retry)
        else:
            update_min_unacked_psn = None
            rc_op = resp[BTH].opcode
            if RC.read_resp(rc_op):
                update_min_unacked_psn = self.handle_read_resp(resp)
            elif rc_op == RC.ATOMIC_ACKNOWLEDGE:
                update_min_unacked_psn = self.handle_atomic_ack(resp)
            elif rc_op == RC.ACKNOWLEDGE:
                update_min_unacked_psn = self.handle_ack(resp)
            else:
                raise Exception(f'unsupported response opcode={rc_op}')

            if update_min_unacked_psn:
                self.min_unacked_psn = Util.next_psn(resp[BTH].psn) # TODO: delete acked request packets
        logging.debug(f'min unacked psn={self.min_unacked_psn}, next psn={self.sq_psn}')

    def retry(self, psn_begin_retry):
        pre_retry_psn = psn_begin_retry
        for retry_psn in Util.psn_range(psn_begin_retry, self.sq_psn):
            if retry_psn in self.req_pkt_dict:
                pkt_to_retry, retry_wr_ssn = self.req_pkt_dict[retry_psn]
                self.send_pkt(retry_wr_ssn, pkt_to_retry, save_pkt = False)
                pre_retry_psn = retry_psn
            else:
                pre_pkt_to_retry, pre_retry_wr_ssn = self.req_pkt_dict[pre_retry_psn]
                assert pre_pkt_to_retry[BTH].opcode == RC.RDMA_READ_REQUEST, 'should be read request asking for multiple responses'
                retry_read_wr = self.send_wqe_dict[pre_retry_wr_ssn]
                assert retry_read_wr.op() == WR_OPCODE.RDMA_READ, 'should be read WR asking for multiple responses'
                assert retry_read_wr.len() > self.pmtu, 'read WR DMA length should > PMTU'

    def ack_send_or_write_req(self, psn_to_ack):
        pkt_to_ack, pending_wr_ssn = self.req_pkt_dict[psn_to_ack]
        rc_op = pkt_to_ack[BTH].opcode
        
        if rc_op == RC.RDMA_READ_REQUEST or RC.atomic(rc_op):
            return False

        # Generate CQE if the packet to ack is the last one
        if RC.last_req_pkt(rc_op) or RC.only_req_pkt(rc_op):
            send_or_write_wr = self.send_wqe_dict[pending_wr_ssn]
            # Generate CQE for each acked send or write WR
            cqe = CQE(
                wr_id = send_or_write_wr.id(),
                status = WC_STATUS.SUCCESS,
                opcode = WC_OPCODE.from_rc_op(rc_op),
                length = send_or_write_wr.len(),
                qpn = self.sqpn(),
                src_qp = self.dqpn(),
                wc_flags = 0, # Requester side CQE no need to handle IBV_WC_WITH_IMM or IBV_WC_WITH_INV
            )
            # No need to retire top RQ element since this is request side, no RQ logic involved
            self.cq.push(cqe)
            # Delete completed send WR
            del self.send_wqe_dict[pending_wr_ssn]
        return True

    def coalesce_ack(self, psn_upper_limit): # psn_upper_limit not included
        assert Util.psn_compare(self.min_unacked_psn, psn_upper_limit, self.sq_psn) <= 0, 'min_unacked_psn shoud <= psn_upper_limit'
        cur_psn = self.min_unacked_psn
        for unacked_psn in Util.psn_range(self.min_unacked_psn, psn_upper_limit):
            ack_res = self.ack_send_or_write_req(unacked_psn)
            if not ack_res: # unacked_psn is either read or atomic request, coalesce ack should stop
                return (False, unacked_psk)
        # cur_psn = self.min_unacked_psn
        # for unacked_psn in Util.psn_range(self.min_unacked_psn, psn_upper_limit):
        #     unacked_pkt, pending_wr_ssn = self.req_pkt_dict[unacked_psn]
        #     rc_op = unacked_pkt[BTH].opcode
        #     cur_psn = unacked_psn
        #     # Handle implicitly unack read and atomic operations
        #     if rc_op == RC.RDMA_READ_REQUEST or RC.atomic(rc_op):
        #         return (False, cur_psn)

        #     # Implicitly ack previous send and write operations
        #     if RC.last_req_pkt(rc_op) or RC.only_req_pkt(rc_op):
        #         sr = self.send_wqe_dict[pending_wr_ssn]
        #         # Generate CQE for each implicitly acked send or write WR
        #         cqe = CQE(
        #             wr_id = sr.id(),
        #             status = WC_STATUS.SUCCESS,
        #             opcode = WC_OPCODE.from_rc_op(rc_op),
        #             length = sr.len(),
        #             qpn = self.sqpn(),
        #             src_qp = self.dqpn(),
        #             wc_flags = 0, # Requester side CQE no need to handle IBV_WC_WITH_IMM or IBV_WC_WITH_INV
        #         )
        #         # No need to retire top RQ element since this is request side, no RQ logic involved
        #         self.cq.push(cqe)
        #         # Delete completed send WR # TODO: delete acked request packets
        #         del self.send_wqe_dict[pending_wr_ssn]
        return (True, psn_upper_limit)

    def handle_ack(self, ack):
        assert ack[BTH].opcode == RC.ACKNOWLEDGE, 'should be ack response'

        # AETH.code {0: "ACK", 1: "RNR", 2: "RSVD", 3: "NAK"}
        if ack[AETH].code == 0: # ACK
            ack_res = self.ack_send_or_write_req(ack[BTH].psn)
            assert ack_res, 'should successfully ack send or write request'
            return True # ACK should update unacked_min_psn
        elif ack[AETH].code == 3 and ack[AETH].value in [1, 2, 3]: # NAK invalid request / remote access / remote operation error, no retry
            self.qps = QPS.ERR
            self.coalesce_ack(ack[BTH].psn)
            # Explicitly NAK corresponding request
            nak_psn, nak_ssn = self.req_pkt_dict[ack[BTH].psn]
            nak_sr = self.send_wqe_dict[nak_ssn]
            nak_cqe = CQE(
                wr_id = nak_sr.id(),
                status = WC_STATUS.from_nak(ack[AETH].value),
                opcode = WC_OPCODE.from_wr_op(nak_sr.id()),
                length = nak_sr.len(),
                qpn = self.sqpn(),
                src_qp = self.dqpn(),
                wc_flags = 0,
            )
            self.cq.push(nak_cqe)
            del self.send_wqe_dict[nak_psn]

            # All pending processing send WR will be completed with flush in error
            # Since current implementation is single-thread, this case does not matter
            for pending_ssn, pending_sr in self.send_wqe_dict.items():
                rc_op = unacked_pkt[BTH].opcode
                flush_pending_cqe = CQE(
                    wr_id = pending_sr.id(),
                    status = WC_STATUS.WR_FLUSH_ERR,
                    opcode = WC_OPCODE.from_wr_op(pending_sr.id()),
                    length = nak_sr.len(),
                    qpn = self.sqpn(),
                    src_qp = self.dqpn(),
                    wc_flags = 0,
                )
                self.cq.push(flush_pending_cqe)
            self.send_wqe_dict.clear()

            # All submitted WR in SQ will be completed with flush in error
            while not self.empty():
                flush_sr = self.pop()
                flush_cqe = CQE(
                    wr_id = flush_sr.id(),
                    status = WC_STATUS.WR_FLUSH_ERR,
                    opcode = WC_OPCODE.from_wr_op(flush_sr.id()),
                    length = flush_sr.len(),
                    qpn = self.sqpn(),
                    src_qp = self.dqpn(),
                    wc_flags = 0,
                )
                self.cq.push(flush_cqe)
        elif ack[AETH].code == 1: # RNR NAK, should retry
            rnr_psn = ack[BTH].psn
            assert rnr_psn in self.req_pkt_dict, 'the PSN of RNR NAK should be of a request sent by SQ and saved in req_pkt_dict'
            rnr_pkt, rnr_wr_ssn = self.req_pkt_dict[rnr_psn]
            self.send_pkt(rnr_wr_ssn, rnr_pkt, save_pkt = False)

        elif (ack[AETH].code == 3 and ack[AETH].value == 0): # NAK seq error, should retry
            seq_err_psn = ack[BTH].psn
            if seq_err_psn not in self.req_pkt_dict: # the seq_err_psn is in the middle of read responses, start retry partial read
                assert self.cur_read_resp_ctx is not None, 'NAK sequence error occured in the middle of read responses, should have read response context'
                orig_read_req_psn = self.cur_read_resp_ctx.orig_read_req_psn
                orig_read_req, orig_read_wr_ssn = self.req_pkt_dict[orig_read_req_psn]
                assert orig_read_wr_ssn == self.cur_read_resp_ctx.read_ssn, 'orig_read_wr_ssn shoud == self.cur_read_resp_ctx.read_ssn'
                # Build a new read request, but its PSN is within the range of the read response to the original read request
                retry_read_req = deepcopy(orig_read_req)
                read_offset = self.cur_read_resp_ctx.read_offset
                retry_read_req[BTH].psn = seq_err_psn
                retry_read_req[RETH].va += read_offset
                retry_read_req[RETH].dlen -= read_offset
                self.send_pkt(orig_read_wr_ssn, retry_read_req, save_pkt = True) # Since the retried read request is a new one, it should be saved in req_pkt_dict
                remaining_read_resp_pkt_num = math.ceil(retry_read_req[RETH].dlen / self.pmtu)
                seq_err_psn = (seq_err_psn + remaining_read_resp_pkt_num) % MAX_PSN
            self.retry(seq_err_psn) # retry remaining request if any
        else:
            logging.debug('received reserved AETH code or reserved AETH NAK value or unsported AETH NAK value: ' + ask.show(dump = True))
        return False # No ACK-ed packet, do not update unacked_min_psn

    def handle_read_resp(self, read_resp):
        rc_op = read_resp[BTH].opcode
        assert RC.read_resp(rc_op), 'should be read response'
        # TODO: handle locally detected error: Length error / Requester Class B
        assert Util.check_pkt_size(self.pmtu, read_resp), 'received packet size illegal'
        # TODO: handle locally detected error: Local Memory Protection Error / Requester Class B
        assert Util.check_op_with_access_flags(rc_op, self.access_flags), 'received packet has opcode without proper permission'

        if rc_op == RC.RDMA_READ_RESPONSE_FIRST or rc_op == RC.RDMA_READ_RESPONSE_ONLY:
            read_req_psn = read_resp[BTH].psn
            read_req, read_req_ssn = self.req_pkt_dict[read_req_psn]
            read_wr = self.send_wqe_dict[read_req_ssn]
            read_resp_lkey = read_wr.lkey()
            read_resp_laddr = read_wr.laddr()
            read_req_dlen = read_req[RETH].dlen

            read_mr = None
            if read_req_dlen > 0:
                # TODO: handle locally detected error: Length Error / Requester Class B
                assert self.pd.validate_mr(rc_op, read_resp_lkey, read_resp_laddr, read_req_dlen), 'read response local access error'
                read_mr = self.pd.get_mr(read_resp_lkey)
                if self.cur_read_resp_ctx is None:
                    self.cur_read_resp_ctx = ReadRespCtx(
                        read_mr = read_mr,
                        read_dlen = read_req_dlen,
                        read_laddr = read_resp_laddr,
                        read_offset = 0,
                        read_wr_id = read_wr.id(),
                        read_ssn = read_req_ssn,
                        orig_read_req_psn = read_req_psn,
                    )
                else:
                    logging.debug(f'read request of PSN={self.cur_read_resp_ctx.orig_read_req_psn} is retried, the retried read request PSN={read_req_psn}')

        read_mr = self.cur_read_resp_ctx.read_mr
        read_dlen = self.cur_read_resp_ctx.read_dlen
        read_laddr = self.cur_read_resp_ctx.read_laddr
        read_offset = self.cur_read_resp_ctx.read_offset
        if Raw in read_resp:
            read_mr.write(read_resp[Raw].load, addr = read_laddr + read_offset)
            read_offset += len(read_resp[Raw].load)
        # Update read_offset to cur_read_resp_ctx
        self.cur_read_resp_ctx.read_offset = read_offset

        if rc_op == RC.RDMA_READ_RESPONSE_LAST or rc_op == RC.RDMA_READ_RESPONSE_ONLY:
            # TODO: handle locally detected error: Length error / Requester Class B
            assert read_offset == read_dlen, 'read response data size not match DMA length'
            read_wr_id = self.cur_read_resp_ctx.read_wr_id
            read_ssn = self.cur_read_resp_ctx.read_ssn
            self.cur_read_resp_ctx = None # Reset cur_read_resp_ctx to None after receive the last or only read response

            # Generate CQE for read response
            read_cqe = CQE(
                wr_id = read_wr_id,
                status = WC_STATUS.SUCCESS,
                opcode = WC_OPCODE.from_rc_op(rc_op),
                length = read_dlen,
                qpn = self.sqpn(),
                src_qp = self.dqpn(),
                wc_flags = 0,
            )
            # No need to retire top RQ element since this is requester side, no RQ logic involved
            self.cq.push(read_cqe)
            # Delete completed read WR
            del self.send_wqe_dict[read_ssn]
        return True # Should update unacked_min_psn

    def handle_atomic_ack(self, atomic_ack):
        rc_op = atomic_ack[BTH].opcode
        assert rc_op == RC.ATOMIC_ACKNOWLEDGE, 'should be atomic ack'
        # TODO: handle atomic NAK, does atomic have NAK?
        assert atomic_ack[AETH].code == 0, 'atomic ack is NAK'

        atomic_req, atomic_wr_ssn = self.req_pkt_dict[atomic_ack[BTH].psn]
        atomic_wr = self.send_wqe_dict[atomic_wr_ssn]
        atomic_laddr = atomic_wr.laddr()
        atomic_lkey = atomic_wr.lkey()

        # TODO: handle locally detected error: Local Memory Protection Error / Requester Class B
        assert self.pd.validate_mr(rc_op, atomic_lkey, atomic_laddr, ATOMIC_BYTE_SIZE), 'atomic response local access error'
        atomic_mr = self.pd.get_mr(atomic_lkey)
        atomic_mr.write(byte_data = atomic_ack[AtomicAckETH].orig, addr = atomic_laddr)
        atomic_cqe = CQE(
            wr_id = atomic_wr.id(),
            status = WC_STATUS.SUCCESS,
            opcode = WC_OPCODE.from_rc_op(rc_op),
            length = ATOMIC_BYTE_SIZE,
            qpn = self.sqpn(),
            src_qp = self.dqpn(),
            wc_flags = 0,
        )
        # No need to retire top RQ element since this is request side, no RQ logic involved
        self.cq.push(atomic_cqe)
        # Delete completed atomic WR
        del self.send_wqe_dict[atomic_wr_ssn]
        return True # Should update unacked_min_psn

class RQ:
    def __init__(self, pd, cq, sq, qpn, rq_psn, pmtu, access_flags, use_ipv6,
        pkey = PKEY_DEFAULT,
        max_rd_atomic = 10,
        max_dest_rd_atomic = 10,
        min_rnr_timer = 10,
        timeout = 10,
        retry_cnt = 3,
        rnr_rery = 3,
    ):
        self.rq = []
        self.qps = QPS.INIT
        self.pd = pd # TODO: check pd match for each req
        self.cq = cq
        self.sq = sq
        self.qpn = qpn
        self.rq_psn = rq_psn % MAX_PSN
        self.pmtu = pmtu
        self.msn = 0

        self.dgid = None
        self.dst_qpn = None
        self.access_flags = access_flags
        self.pkey = pkey
        self.max_rd_atomic = max_rd_atomic
        self.max_dest_rd_atomic = max_dest_rd_atomic
        self.min_rnr_timer = min_rnr_timer
        self.timeout = timeout
        self.retry_cnt = retry_cnt
        self.rnr_rery = rnr_rery

        self.use_ipv6 = use_ipv6
        self.resp_pkt_dict = {}
        self.pre_pkt_op = None

        self.cur_send_req_ctx = None
        self.cur_write_req_ctx = None

    def modify(self,
        qps = None,
        pmtu = None,
        rq_psn = None,
        dgid = None,
        dst_qpn = None,
        access_flags = None,
        pkey = None,
        sq_draining = None,
        max_rd_atomic = None,
        max_dest_rd_atomic = None,
        min_rnr_timer = None,
        timeout = None,
        retry_cnt = None,
        rnr_rery = None,
    ):
        if qps:
            self.qps = qps
        if pmtu:
            self.pmtu = pmtu
        if rq_psn:
            self.rq_psn = rq_psn % MAX_PSN
        if dgid:
            self.dgid = dgid
        if dst_qpn:
            self.dst_qpn = dst_qpn # qpn in number instread of hex string
        if access_flags:
            self.access_flags = access_flags
        if pkey:
            self.pkey = pkey
        if sq_draining:
            self.sq_draining = sq_draining
        if max_rd_atomic:
            self.max_rd_atomic = max_rd_atomic
        if max_dest_rd_atomic:
            self.max_dest_rd_atomic = max_dest_rd_atomic
        if min_rnr_timer:
            self.min_rnr_timer = min_rnr_timer
        if timeout:
            self.timeout = timeout
        if retry_cnt:
            self.retry_cnt = retry_cnt
        if rnr_rery:
            self.rnr_rery = rnr_rery

    def push(self, wr):
        self.rq.append(wr)

    def pop(self):
        return self.rq.pop(0)

    def top(self):
        return self.rq[0]

    def empty(self):
        return not bool(self.rq)

    def sqpn(self):
        return self.qpn

    def dqpn(self):
        return self.dst_qpn

    def get_qp_access_flags(self):
        return self.access_flags

    def is_expected_req(self, req_psn):
        return req_psn == self.rq_psn

    def handle_dup_or_illegal_req(self, req):
        req_psn = req[BTH].psn
        psn_comp_res = Util.psn_compare(self.rq_psn, req_psn, self.rq_psn)
        assert psn_comp_res != 0, 'should handle duplicate or illegal request'
        if psn_comp_res > 0: # Dup req
            logging.debug(f'RQ={self.sqpn()} received duplicate request: ' + req.show(dump = True))
            rc_op = req[BTH].opcode
            if RC.send(rc_op) or RC.write(rc_op):
                dup_resp = self.resp_pkt_dict[req_psn]
                dup_resp[BTH].psn = self.rq_psn # Dup requst response has latest PSN
                self.send_pkt(dup_resp, save_pkt = False)
            elif rc_op == RC.RDMA_READ_REQUEST:
                self.handle_read_req(self, req, update_epsn = False)
            elif RC.atomic(rc_op):
                # TODO: check the dup atomic request is the same as before
                dup_resp = self.resp_pkt_dict[req_psn]
                if AtomicAckETH in dup_resp:
                    self.send_pkt(dup_resp, save_pkt = False)
                else:
                    logging.debug(f'RQ={self.sqpn()} received duplicate atomic request: ' + req.show(dump = True) + ', but the response was not match: ' + dup_resp.show(dump = True))

        else: # TODO: handle sequence error: Out of Sequence Request Packet / Responder Class B
            logging.debug(f'RQ={self.sqpn()} had sequence error, ePSN={self.rq_psn} but received request: ' + req.show(dump = True))

    def send_pkt(self, resp, save_pkt = True):
        if not self.dqpn():
            raise Exception(f'RQ={self.sqpn()} has no destination QPN')
        elif not self.dgid:
            raise Exception(f'RQ={self.sqpn()} has no destination GID')

        #dst_ipv6 = socket.inet_ntop(socket.AF_INET6, bytes.fromhex(self.dgid))
        dst_ipv6 = socket.inet_ntop(socket.AF_INET6, self.dgid)
        dst_ipv4 = dst_ipv6.replace('::ffff:', '')
        dst_ip = dst_ipv6 if self.use_ipv6 else dst_ipv4
        logging.debug(f'dest IP={dst_ip}')

        pkt = IP(dst=dst_ip)/UDP(dport=ROCE_PORT, sport=self.sqpn())/resp
        cpsn = pkt[BTH].psn
        if save_pkt:
            self.resp_pkt_dict[cpsn] = pkt
        logging.debug(f'RQ={self.sqpn()} response: ' + pkt.show(dump = True))
        send(pkt)

    def recv_pkt(self, pkt):
        logging.debug(f'RQ={self.sqpn()} received packet with length={len(pkt)}: ' + pkt.show(dump = True) + f', previous operation is: {self.pre_pkt_op}')
        rc_op = pkt[BTH].opcode

        # TODO: handle head verification
        assert pkt[BTH].dqpn == self.qpn, 'received packet QPN not match'
        assert pkt[BTH].opcode < 0x20, 'only RC supported'
        assert pkt[BTH].version == 0, 'header version must be zero'

        # TODO: handle invalid request error: Out of Sequence OpCode / Responder Class C
        assert Util.check_pre_cur_ops(self.pre_pkt_op, rc_op), 'previous and current opcodes are not legal'

        if RC.request(rc_op):
            # TODO: handle invalid request error: Length errors / Responder Class C
            assert Util.check_pkt_size(self.pmtu, pkt), 'received packet size illegal'
            # TODO: handle invalid request error: Unsupported or Reserved OpCode / Responder Class C
            assert Util.check_op_with_access_flags(rc_op, self.access_flags), 'received packet has opcode without proper permission'

            if self.is_expected_req(pkt[BTH].psn):
                if RC.send(rc_op):
                    self.handle_send_req(pkt)
                elif RC.write(rc_op):
                    self.handle_write_req(pkt)
                elif rc_op == RC.RDMA_READ_REQUEST:
                    self.handle_read_req(pkt)
                elif RC.atomic(rc_op):
                    self.handle_atomic_req(pkt)
                else:
                    raise Exception(f'unknown request opcode={rc_op}')
                self.rq_psn = self.rq_psn % MAX_PSN
            else:
                self.handle_dup_or_illegal_req(pkt)
            self.pre_pkt_op = rc_op
        elif RC.response(rc_op):
            if self.sq.is_expected_resp(pkt[BTH].psn):
                self.sq.handle_normal_resp(pkt)
                self.pre_pkt_op = rc_op
            else:
                self.sq.handle_dup_or_illegal_resp(pkt)
                # Do not update pre_pkt_op for duplicate packet or ghost response
        else:
            raise Exception(f'unsupported opcode={rc_op}')

    def handle_send_req(self, send_req):
        rc_op = send_req[BTH].opcode
        assert RC.send(rc_op), 'should be send request'

        # TODO: handle send request has no data
        if RC.first_req_pkt(rc_op) or RC.only_req_pkt(rc_op):
            # TODO: handle RNR NAK: Resources Not Ready Error / Responder Class B
            assert not self.empty(), 'RQ empty'
            rr = self.pop()
            self.cur_send_req_ctx = (rr, 0)

        rr, send_offset = self.cur_send_req_ctx
        send_addr = rr.addr() + send_offset
        data_size = 0
        if Raw in send_req:
            data_size = len(send_req[Raw].load)
            # TODO: handle invalid request error: Length errors / Responder Class C
            assert self.pd.validate_mr(rc_op, rr.lkey(), send_addr, data_size), 'no enough receive buffer for send request'
            send_mr = self.pd.get_mr(rr.lkey())
            send_mr.write(send_req[Raw].load, addr = send_addr)
            send_offset += len(send_req[Raw].load)
        self.cur_send_req_ctx = (rr, send_offset)

        if RC.last_req_pkt(rc_op) or RC.only_req_pkt(rc_op):
            #self.pop()
            self.msn = (self.msn + 1) % MAX_MSN
            self.cur_send_req_ctx = None # Reset cur_send_req_ctx to None after receive the last or only send request

            cqe_wc_flags = 0
            cqe_imm_data_or_inv_rkey = None
            if RC.has_imm(rc_op):
                cqe_wc_flags |= WC_FLAGS.WITH_IMM
                cqe_imm_data_or_inv_rkey = send_req[ImmDt].data
            elif RC.has_inv(rc_op):
                cqe_wc_flags |= WC_FLAGS.WITH_INV
                cqe_imm_data_or_inv_rkey = send_req[IETH].rkey # TODO: handle rkey invalidation
            # Generate CQE for received send request
            cqe = CQE(
                wr_id = rr.id(),
                status = WC_STATUS.SUCCESS,
                opcode = WC_OPCODE.from_rc_op(rc_op),
                length = send_offset,
                qpn = self.sqpn(),
                src_qp = self.dqpn(),
                wc_flags = cqe_wc_flags,
                imm_data_or_inv_rkey = cqe_imm_data_or_inv_rkey,
            )
            self.cq.push(cqe)

            if send_req[BTH].solicited:
                # TODO: handle solicited event
                TODO
        self.rq_psn = (self.rq_psn + 1) % MAX_PSN # Update ePSN
        if send_req[BTH].ackreq:
            self.process_ack(send_req)

    def handle_write_req(self, write_req):
        rc_op = write_req[BTH].opcode
        assert RC.write(rc_op), 'should be write request'

        write_req_rkey = None
        write_req_addr = None
        write_req_dlen = None
        if RC.first_req_pkt(rc_op) or RC.only_req_pkt(rc_op):
            if RC.has_imm(rc_op):
                write_req_rkey = write_req[RETHImmDt].rkey
                write_req_addr = write_req[RETHImmDt].va
                write_req_dlen = write_req[RETHImmDt].dlen
            else:
                write_req_rkey = write_req[RETH].rkey
                write_req_addr = write_req[RETH].va
                write_req_dlen = write_req[RETH].dlen

            write_mr = None
            if write_req_dlen > 0:
                # TODO: handle remote access error: Responder Class C
                assert self.pd.validate_mr(rc_op, write_req_rkey, write_req_addr, write_req_dlen), 'write request remote access error'
                write_mr = self.pd.get_mr(write_req_rkey)
            else:
                assert Raw not in write_req and RC.only_req_pkt(rc_op), 'write request with DMA length as 0 should have no data'
            self.cur_write_req_ctx = (write_mr, write_req_dlen, write_req_addr, 0)

        write_mr, write_dlen, write_addr, write_offset = self.cur_write_req_ctx
        if Raw in write_req:
            write_mr.write(write_req[Raw].load, addr = write_addr + write_offset)
            write_offset += len(write_req[Raw].load)
        # Update write_offset to cur_write_req_ctx
        self.cur_write_req_ctx = (write_mr, write_dlen, write_addr, write_offset)

        if RC.last_req_pkt(rc_op) or RC.only_req_pkt(rc_op):
            # TODO: handle invalid request error: Length error / Responder Class C
            assert write_offset == write_dlen, 'write request data size not match DMA length'

            self.msn = (self.msn + 1) % MAX_MSN
            self.cur_write_req_ctx = None # Reset cur_write_req_ctx to None after receive the last or only write request

            if RC.has_imm(rc_op):
                cqe_wc_flags = WC_FLAGS.WITH_IMM
                cqe_imm_data = write_req[RETHImmDt].data
                # TODO: handle RNR NAK: Resources Not Ready Error / Responder Class B
                assert not self.empty(), 'RQ is empty but write with immediate data needs to consume a receive WR'
                rr = self.pop()
                # Generate CQE for received send request
                cqe = CQE(
                    wr_id = rr.id(),
                    status = WC_STATUS.SUCCESS,
                    opcode = WC_OPCODE.from_rc_op(rc_op),
                    length = write_dlen,
                    qpn = self.sqpn(),
                    src_qp = self.dqpn(),
                    wc_flags = cqe_wc_flags,
                    imm_data_or_inv_rkey = cqe_imm_data,
                )
                self.cq.push(cqe)

                if write_req[BTH].solicited:
                    # TODO: handle solicited event
                    TODO
        self.rq_psn = (self.rq_psn + 1) % MAX_PSN # Update ePSN
        if write_req[BTH].ackreq:
            self.process_ack(write_req)

    def handle_read_req(self, read_req, update_epsn = True):
        rc_op = read_req[BTH].opcode
        assert rc_op == RC.RDMA_READ_REQUEST, 'should be read request'

        read_req_size = read_req[RETH].dlen
        read_req_addr = read_req[RETH].va
        read_req_rkey = read_req[RETH].rkey

        read_data = None
        if read_req_size > 0:
            # TODO: handle remote access error: Responder Class C
            assert self.pd.validate_mr(rc_op, read_req_rkey, read_req_addr, read_req_size), 'read request remote access error'
            read_mr = self.pd.get_mr(read_req_rkey)
            read_data = read_mr.read(addr = read_req_addr, size = read_req_size)

        cpsn = self.rq_psn
        dqpn = self.dqpn()
        self.msn = (self.msn + 1) % MAX_MSN
        read_resp_pkt_num = math.ceil(read_req_size / self.pmtu) if read_req_size > 0 else 1
        read_aeth = AETH(code = 'ACK', value = CREDIT_CNT_INVALID, msn = self.msn)
        if read_resp_pkt_num > 1:
            read_resp_bth = BTH(
                opcode = RC.RDMA_READ_RESPONSE_FIRST,
                psn = cpsn,
                dqpn = dqpn,
            )
            read_resp = read_resp_bth/read_aeth/Raw(load = read_data[0 : self.pmtu])
            self.send_pkt(read_resp, save_pkt = False)

            read_resp_mid_pkt_num = read_resp_pkt_num - 2
            for i in range(read_resp_mid_pkt_num):
                read_resp_bth = BTH(
                    opcode = RC.RDMA_READ_RESPONSE_MIDDLE,
                    psn = cpsn + i + 1,
                    dqpn = dqpn,
                )
                read_resp = read_resp_bth/Raw(load = read_data[((i + 1) * self.pmtu) : ((i + 2) * self.pmtu)])
                self.send_pkt(read_resp, save_pkt = False)

        rc_op = None
        if read_resp_pkt_num == 1:
            rc_op = RC.RDMA_READ_RESPONSE_ONLY
        else:
            rc_op = RC.RDMA_READ_RESPONSE_LAST
        read_resp_bth = BTH(
            opcode = rc_op,
            psn = cpsn + read_resp_pkt_num - 1,
            dqpn = dqpn,
        )
        read_resp = read_resp_bth/read_aeth
        if read_req_size > 0:
            read_resp = read_resp/Raw(load = read_data[((read_resp_pkt_num - 1) * self.pmtu) : read_req_size])
        self.send_pkt(read_resp, save_pkt = False)
        if update_epsn:
            self.rq_psn = (self.rq_psn + read_resp_pkt_num) % MAX_PSN

    def handle_atomic_req(self, atomic_req):
        rc_op = atomic_req[BTH].opcode
        assert RC.atomic(rc_op), 'should be atomic request'

        atomic_req_rkey = atomic_req[AtomicETH].rkey
        atomic_req_addr = atomic_req[AtomicETH].va
        # TODO: handle remote access error: Responder Class C
        assert self.pd.validate_mr(rc_op, atomic_req_rkey, atomic_req_addr, ATOMIC_BYTE_SIZE), 'atomic request remote access error'
        mr = self.pd.get_mr(atomic_req_rkey)

        cpsn = self.rq_psn
        dqpn = self.dqpn()
        self.msn = (self.msn + 1) % MAX_MSN

        # TODO: handle invalid request error: Misaligned ATOMIC / Responder Class C
        assert Util.check_addr_aligned(addr = atomic_req[AtomicETH].va, mr = mr), 'atomic request address is not 8-byte aligned'
        orig = int.from_bytes(mr.read(addr = atomic_req_addr, size = ATOMIC_BYTE_SIZE), sys.byteorder)
        comp = atomic_req[AtomicETH].comp
        swap = atomic_req[AtomicETH].swap
        if rc_op == RC.COMPARE_SWAP:
            if orig == comp:
                mr.write(byte_data = swap.to_bytes(ATOMIC_BYTE_SIZE, sys.byteorder), addr = atomic_req_addr)
        else:
            mr.write(byte_data = (orig + comp).to_bytes(ATOMIC_BYTE_SIZE, sys.byteorder), addr = atomic_req_addr)

        ack_bth = BTH(
            opcode = RC.ATOMIC_ACKNOWLEDGE,
            psn = cpsn,
            dqpn = dqpn,
        )
        ack_aeth = AETH(code = 'ACK', value = CREDIT_CNT_INVALID, msn = self.msn)
        atomic_ack_eth = AtomicAckETH(orig = orig)
        atomic_ack = ack_bth/ack_aeth/atomic_ack_eth
        self.send_pkt(atomic_ack)
        self.rq_psn = (self.rq_psn + 1) % MAX_PSN # Update ePSN

    def process_ack(self, req):
        assert req[BTH].ackreq, 'received request should ask for ack response'
        ack_bth = BTH(
            opcode = RC.ACKNOWLEDGE,
            psn = req[BTH].psn,
            dqpn = self.dqpn(),
        )
        # TODO: RQ flow control
        ack = ack_bth/AETH(code = 'ACK', value = CREDIT_CNT_INVALID, msn = self.msn)
        self.send_pkt(ack)

class QP:
    def __init__(self, pd, cq, qpn, pmtu, access_flags, use_ipv6,
        rq_psn = 0,
        sq_psn = 0,
        pkey = PKEY_DEFAULT,
        sq_draining = 0,
        max_rd_atomic = 10,
        max_dest_rd_atomic = 10,
        min_rnr_timer = 10,
        timeout = 10,
        retry_cnt = 3,
        rnr_rery = 3,
    ):
        self.cq = cq
        self.sq = SQ(
            pd = pd,
            cq = cq,
            qpn = qpn,
            sq_psn = sq_psn,
            pmtu = pmtu,
            access_flags = access_flags,
            use_ipv6 = use_ipv6,
            pkey = pkey,
            draining = sq_draining,
            max_rd_atomic = max_rd_atomic,
            max_dest_rd_atomic = max_dest_rd_atomic,
            min_rnr_timer = min_rnr_timer,
            timeout = timeout,
            retry_cnt = retry_cnt,
            rnr_rery = rnr_rery,
        )
        self.rq = RQ(
            pd = pd,
            cq = cq,
            sq = self.sq,
            qpn = qpn,
            rq_psn = rq_psn,
            pmtu = pmtu,
            access_flags = access_flags,
            use_ipv6 = use_ipv6,
            pkey = pkey,
            max_rd_atomic = max_rd_atomic,
            max_dest_rd_atomic = max_dest_rd_atomic,
            min_rnr_timer = min_rnr_timer,
            timeout = timeout,
            retry_cnt = retry_cnt,
            rnr_rery = rnr_rery,
        )
        pd.add_qp(self)

    def modify_qp(self,
        qps = None,
        pmtu = None,
        rq_psn = None,
        sq_psn = None,
        dgid = None,
        dst_qpn = None,
        access_flags = None,
        pkey = None,
        sq_draining = None,
        max_rd_atomic = None,
        max_dest_rd_atomic = None,
        min_rnr_timer = None,
        timeout = None,
        retry_cnt = None,
        rnr_rery = None,
    ):
        self.sq.modify(
            qps = qps,
            pmtu = pmtu,
            sq_psn = sq_psn,
            dgid = dgid,
            dst_qpn = dst_qpn,
            access_flags = access_flags,
            pkey = pkey,
            sq_draining = sq_draining,
            max_rd_atomic = max_rd_atomic,
            max_dest_rd_atomic = max_dest_rd_atomic,
            min_rnr_timer = min_rnr_timer,
            timeout = timeout,
            retry_cnt = retry_cnt,
            rnr_rery = rnr_rery,
        )
        self.rq.modify(
            qps = qps,
            pmtu = pmtu,
            rq_psn = rq_psn,
            dgid = dgid,
            dst_qpn = dst_qpn,
            access_flags = access_flags,
            pkey = pkey,
            sq_draining = sq_draining,
            max_rd_atomic = max_rd_atomic,
            max_dest_rd_atomic = max_dest_rd_atomic,
            min_rnr_timer = min_rnr_timer,
            timeout = timeout,
            retry_cnt = retry_cnt,
            rnr_rery = rnr_rery,
        )

    def qpn(self):
        return self.sq.sqpn()

    def recv_pkt(self, pkt):
        self.rq.recv_pkt(pkt)

    def poll_cq(self):
        if not self.cq.empty():
            return self.cq.pop()
        else:
            return None

    def post_send(self, send_wr):
        self.sq.push(send_wr)

    def post_recv(self, recv_wr):
        self.rq.push(recv_wr)

    def process_one_sr(self):
        self.sq.process_one()

class RoCEv2:
    def __init__(self, pmtu = PMTU.MTU_256, use_ipv6 = False, recv_timeout_secs = 1):
        self.roce_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        roce_bind_addr = ('0.0.0.0', ROCE_PORT)
        self.roce_sock.bind(roce_bind_addr)
        self.pmtu = pmtu
        self.use_ipv6 = use_ipv6
        self.recv_timeout_secs = recv_timeout_secs
        self.cur_cqn = 0
        self.cur_pdn = 0
        self.cur_qpn = 2
        self.cq_dict = {}
        self.pd_dict = {}
        self.qp_dict = {}

    def alloc_pd(self):
        pdn = self.cur_pdn
        self.cur_pdn += 1
        pd = PD(pdn)
        self.pd_dict[pdn] = pd
        return pd

    def create_cq(self):
        cqn = self.cur_cqn
        self.cur_cqn += 1
        cq = CQ(cqn)
        self.cq_dict[cqn] = cq
        return cq

    def create_qp(self, pd, cq, access_flags):
        qpn = self.cur_qpn
        self.cur_qpn += 1
        qp = QP(pd = pd, cq = cq, qpn = qpn, access_flags = access_flags, pmtu = self.pmtu, use_ipv6 = self.use_ipv6)
        self.qp_dict[qpn] = qp
        return qp

    def mtu(self):
        return self.pmtu

    def recv_pkts(self, npkt = 1):
        for i in range(npkt):
            # TODO: handle retry
            self.roce_sock.settimeout(self.recv_timeout_secs)
            roce_bytes, peer_addr = self.roce_sock.recvfrom(UDP_BUF_SIZE)
            roce_pkt = BTH(roce_bytes)
            # TODO: handle head verification, wrong QPN
            local_qp = self.qp_dict[roce_pkt.dqpn]
            local_qp.recv_pkt(roce_pkt)
        logging.debug(f'received {npkt} RoCE packets')