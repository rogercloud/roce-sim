import argparse
import logging
import socket
import struct
from roce_enum import *
from roce_v2 import *

POS_IN_MR = 8
MR_SIZE = 1024
MSG_SIZE = MR_SIZE - POS_IN_MR

SRC_PORT = 9527

S_VA = '000056482bb76120'
# S_RKEY = '00000208'
#S_QPN = '00000011'
S_LID = '0000'
#S_GID = '00000000000000000000ffffc0a87aee'

ReceiveReady = 0
SendImm = 1
WriteImm = 2
ReadZero = 3
SendSize = 4
ReadSize = 5
WriteSize = 6
WriteDone = 7
AtomicReady = 8
AtomicDone = 9

parser = argparse.ArgumentParser(description='Input server IP')
parser.add_argument('-s', action='store', dest='src_ip')
arg_res = parser.parse_args()

logging.basicConfig(level=logging.DEBUG)

# Build RoCE data structure
roce = RoCEv2()
pd = roce.alloc_pd()
cq = roce.create_cq()
qp = roce.create_qp(
    pd = pd,
    cq = cq,
    access_flags = ACCESS_FLAGS.LOCAL_WRITE
        | ACCESS_FLAGS.REMOTE_WRITE
        | ACCESS_FLAGS.REMOTE_READ
        | ACCESS_FLAGS.REMOTE_ATOMIC
)
mr = pd.reg_mr(
    va = int(S_VA, 16), length = MR_SIZE,
    access_flags = ACCESS_FLAGS.LOCAL_WRITE
        | ACCESS_FLAGS.REMOTE_WRITE
        | ACCESS_FLAGS.REMOTE_READ
        | ACCESS_FLAGS.REMOTE_ATOMIC
        | ACCESS_FLAGS.ZERO_BASED
)
mr.write(byte_data = b'000000001234567890ABCEDFGHIJKLMNOPQRSTUVWXYZ')

# Wait for connection
udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
server_bind_addr = ('0.0.0.0', SRC_PORT)
udp_sock.bind(server_bind_addr)
exch_data, peer_addr = udp_sock.recvfrom(UDP_BUF_SIZE)
print(struct.unpack('<c', exch_data))
udp_sock.sendto(struct.pack('c', b'2'), peer_addr)

# Send metadata
src_va = '{:016x}'.format(POS_IN_MR)
src_rkey = '{:08x}'.format(mr.rkey())
src_qpn = '{:08x}'.format(qp.qpn())
src_gid = '{0:0>32}'.format('ffff' + socket.inet_aton(arg_res.src_ip).hex())
server_metadata = src_va + src_rkey + src_qpn + S_LID + src_gid
udp_sock.sendto(bytes.fromhex(server_metadata), peer_addr)
# Recive metadata
exch_data, peer_addr = udp_sock.recvfrom(UDP_BUF_SIZE)
parsed_fields = struct.unpack('>QIIH16s', exch_data)
dst_va, dst_rkey, dst_qpn, dst_lid, dst_gid = parsed_fields
print(parsed_fields)

# Setup QP, post receive
sg = SG(pos_in_mr = POS_IN_MR, length = mr.len() - POS_IN_MR, lkey = mr.lkey())
rr = RecvWR(sgl = sg)
qp.post_recv(rr)
qp.modify_qp(qps = QPS.RTR, dgid = dst_gid, dst_qpn = dst_qpn) # dqpn should be integer

# Exchange receive ready
udp_sock.sendto(struct.pack('<i', ReceiveReady), peer_addr)
exch_data, peer_addr = udp_sock.recvfrom(UDP_BUF_SIZE)
print(struct.unpack('<i', exch_data))

qp.modify_qp(qps = QPS.RTS)

# Exchange send imm
udp_sock.sendto(struct.pack('<i', SendImm), peer_addr)
exch_data, peer_addr = udp_sock.recvfrom(UDP_BUF_SIZE)
print(struct.unpack('<i', exch_data))

# RoCE send imm and ack
roce.recv_pkts(1)
cqe = qp.poll_cq()
assert cqe is not None, 'cqe should exist'
assert cqe.local_qpn() == qp.qpn()
assert cqe.sqpn() == dst_qpn
assert cqe.len() == 0
assert cqe.op() == WC_OPCODE.SEND
assert cqe.status() == WC_STATUS.SUCCESS
assert cqe.imm_data_or_inv_rkey() is not None

# Exchange write imm
udp_sock.sendto(struct.pack('<i', WriteImm), peer_addr)
exch_data, peer_addr = udp_sock.recvfrom(UDP_BUF_SIZE)
parsed_fields = struct.unpack('<i', exch_data)
print(parsed_fields)

# RoCE write imm and ack
sg = SG(pos_in_mr = 0, length = 0, lkey = 0)
rr = RecvWR(sgl = sg)
qp.post_recv(rr)
roce.recv_pkts(1)
cqe = qp.poll_cq()
assert cqe is not None, 'cqe should exist'
assert cqe.local_qpn() == qp.qpn()
assert cqe.sqpn() == dst_qpn
assert cqe.len() == 0
assert cqe.op() == WC_OPCODE.RECV_RDMA_WITH_IMM
assert cqe.status() == WC_STATUS.SUCCESS
assert cqe.imm_data_or_inv_rkey() is not None

# Exchange read zero
udp_sock.sendto(struct.pack('<i', ReadZero), peer_addr)
exch_data, peer_addr = udp_sock.recvfrom(UDP_BUF_SIZE)
parsed_fields = struct.unpack('<i', exch_data)
print(parsed_fields)

# RoCE read zero and no ack
roce.recv_pkts(1)
cqe = qp.poll_cq()
assert cqe is None, 'cqe should not exist'

# Exchange send size
send_size = MSG_SIZE
udp_sock.sendto(struct.pack('<iq', SendSize, send_size), peer_addr)
exch_data, peer_addr = udp_sock.recvfrom(UDP_BUF_SIZE)
print(struct.unpack('<iq', exch_data))

# RoCE send and ack
sg = SG(pos_in_mr = POS_IN_MR, length = send_size, lkey = mr.lkey())
sr = SendWR(
    opcode = WR_OPCODE.SEND_WITH_IMM,
    sgl = sg,
    # send_flags = SEND_FLAGS.SIGNALED, No need to ACK
    imm_data_or_inv_rkey = 0x1234,
)
qp.post_send(sr)
qp.process_one_sr()

# Exchange read size
udp_sock.sendto(struct.pack('<iq', ReadSize, -1), peer_addr)
exch_data, peer_addr = udp_sock.recvfrom(UDP_BUF_SIZE)
parsed_fields = struct.unpack('<iq', exch_data)
_, read_size = parsed_fields
print(parsed_fields)

# RoCE read and ack
sg = SG(pos_in_mr = POS_IN_MR, length = read_size, lkey = mr.lkey())
sr = SendWR(
   opcode = WR_OPCODE.RDMA_READ,
   sgl = sg,
   rmt_va = dst_va,
   rkey = dst_rkey,
)
qp.post_send(sr)
read_resp_pkt_num = math.ceil(read_size / roce.mtu()) if read_size > 0 else 1
qp.process_one_sr()
roce.recv_pkts(read_resp_pkt_num * 2) # Retry
cqe = qp.poll_cq()
assert cqe is not None, 'send cqe should exist'
assert cqe.local_qpn() == qp.qpn()
assert cqe.sqpn() == dst_qpn
assert cqe.len() == send_size
assert cqe.op() == WC_OPCODE.SEND
assert cqe.status() == WC_STATUS.SUCCESS
cqe = qp.poll_cq()
assert cqe is not None, 'read cqe should exist'
assert cqe.local_qpn() == qp.qpn()
assert cqe.sqpn() == dst_qpn
#print(f'cqe.len()={cqe.len()} != sg.len()={sg.len()}, read_size={read_size}, cqe.op()={cqe.op()}')
assert cqe.len() == read_size
assert cqe.op() == WC_OPCODE.RDMA_READ
assert cqe.status() == WC_STATUS.SUCCESS

# Exchange write done
udp_sock.sendto(struct.pack('<i', WriteDone), peer_addr)
exch_data, peer_addr = udp_sock.recvfrom(UDP_BUF_SIZE)
parsed_fields = struct.unpack('<i', exch_data)
print(parsed_fields)

exit()

# Exchange read size
read_size = MSG_SIZE
exch_data, peer_addr = udp_sock.recvfrom(UDP_BUF_SIZE)
udp_sock.sendto(struct.pack('<iq', ReadSize, read_size), peer_addr)
parsed_fields = struct.unpack('<iq', exch_data)
_, read_size = parsed_fields
print(parsed_fields)

# RoCE read and ack
mr.write(byte_data = b'00000000abcdefghijklmnopqrstuvwxyz')
roce.recv_pkts(1)

# Exchange write size
exch_data, peer_addr = udp_sock.recvfrom(UDP_BUF_SIZE)
udp_sock.sendto(struct.pack('<iq', WriteSize, -1), peer_addr)
parsed_fields = struct.unpack('<iq', exch_data)
_, write_size = parsed_fields
print(parsed_fields)

# RoCE write and ack
write_req_pkt_num = math.ceil(write_size / roce.mtu()) if write_size > 0 else 1
roce.recv_pkts(write_req_pkt_num)

# Exchange write imm
exch_data, peer_addr = udp_sock.recvfrom(UDP_BUF_SIZE)
udp_sock.sendto(struct.pack('<i', WriteImm), peer_addr)
parsed_fields = struct.unpack('<i', exch_data)
print(parsed_fields)

# RoCE write imm and ack
sg = SG(pos_in_mr = 0, length = 0, lkey = mr.lkey())
sr = SendWR(
    opcode = WR_OPCODE.RDMA_WRITE_WITH_IMM,
    sgl = sg,
    # rmt_va = dst_va,
    # rkey = dst_rkey,
    send_flags = SEND_FLAGS.SIGNALED,
    imm_data_or_inv_rkey = 0x1234,
)
qp.post_send(sr)
qp.process_one_sr()
roce.recv_pkts(1)
cqe = qp.poll_cq()
assert cqe is not None, 'cqe should exist'
assert cqe.local_qpn() == qp.qpn()
assert cqe.sqpn() == dst_qpn
assert cqe.len() == 0
assert cqe.op() == WC_OPCODE.RDMA_WRITE
assert cqe.status() == WC_STATUS.SUCCESS

# Exchange write done
exch_data, peer_addr = udp_sock.recvfrom(UDP_BUF_SIZE)
udp_sock.sendto(struct.pack('<i', WriteDone), peer_addr)
parsed_fields = struct.unpack('<i', exch_data)
print(parsed_fields)

# Exchange atomic ready
exch_data, peer_addr = udp_sock.recvfrom(UDP_BUF_SIZE)
udp_sock.sendto(struct.pack('<i', AtomicReady), peer_addr)
print(struct.unpack('<i', exch_data))

# RoCE atomic and ack
sg = SG(pos_in_mr = POS_IN_MR, length = 8, lkey = mr.lkey())
sr = SendWR(
    opcode = WR_OPCODE.ATOMIC_CMP_AND_SWP,
    sgl = sg,
    rmt_va = dst_va,
    rkey = dst_rkey,
    compare_add = 0,
    swap = 1,
)
qp.post_send(sr)
qp.process_one_sr()
# TODO: soft-roce failed to ack atomic operation
# roce.recv_pkts(1)
# cqe = qp.poll_cq()
# assert cqe is not None, 'cqe should exist'
# assert cqe.local_qpn() == qp.qpn()
# assert cqe.sqpn() == dst_qpn
# assert cqe.len() == sg.len()
# assert cqe.op() == WC_OPCODE.ATOMIC_CMP_AND_SWP
# assert cqe.status() == WC_STATUS.SUCCESS

# Exchange atomic done
exch_data, peer_addr = udp_sock.recvfrom(UDP_BUF_SIZE)
udp_sock.sendto(struct.pack('<i', AtomicDone), peer_addr)
print(struct.unpack('<i', exch_data))

udp_sock.close()
