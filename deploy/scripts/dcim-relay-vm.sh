#!/bin/bash
# DCIM Kafka 中转（**虚拟机侧**，装在 rzkafka / 10.10.250.89）
#
# 背景：对方是每园区一个独立 Kafka 集群，我方服务器不能直连这些 broker IP；
# 中间用一台虚拟机做中转，这台 VM 能直连全部 broker。
#
# 本脚本把「本机 19001~19013 端口」转发到各园区 broker。配合我方服务器侧的
# dcim-relay-server.sh（把发往 broker IP 的包改道到本机这些端口），实现透明的中转：
# 客户端始终以为自己在连 broker 的真实地址，advertised 因此对得上。
#
# 为什么不用隧道：GRE（IP 协议 47）被中间路由器丢弃，静态路由又因跨网段不成立。
# 两端 iptables 改道不依赖任何特殊协议，且无常驻进程。
#
# 安装（已在 10.10.250.89 上执行过）：
#   install -m 755 dcim-relay-vm.sh /usr/local/bin/dcim-relay.sh
#   echo 'net.ipv4.ip_forward = 1' > /etc/sysctl.d/99-dcim-forward.conf
#   /etc/systemd/system/dcim-relay.service（见文件末尾注释）
#
# 端口映射（改 broker 清单时同步改这里与服务器侧那份，序号必须一致）
#   19001 10.4.0.60:9092      廊坊A
#   19002 10.12.251.18:9092   廊坊B1M1
#   19003 10.12.250.18:9092   廊坊B1M2
#   19004 172.20.222.46:9092  平湖A
#   19005 172.20.233.31:9092  平湖B
#   19006 10.10.250.103:40001 惠州A3M1
#   19007 10.10.250.103:40002 惠州A3M2
#   19008 10.10.250.103:40003 惠州A2M1
#   19009 10.10.250.103:40004 惠州A2M2
#   19010 10.10.250.103:40005 重庆
#   19011 10.10.250.103:40006 佛山
#   19012 10.12.249.19:9092   廊坊BM3
#   19013 172.20.233.32:9092  平湖BM3M4
#
# 系统要求：CentOS 7（无需装任何包 —— 只用 sysctl 与 iptables）；FORWARD 策略须为 ACCEPT
#（无 Docker 时即是；装了 Docker 会改成 DROP，那时需额外放行）。

set -u

sysctl -w net.ipv4.ip_forward=1 >/dev/null

# 出接口做 MASQUERADE，让 broker 看到的是本机地址，回程才能回来
iptables -t nat -C POSTROUTING -o eth0 -j MASQUERADE 2>/dev/null \
  || iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE

n=0
for ep in 10.4.0.60:9092 10.12.251.18:9092 10.12.250.18:9092 172.20.222.46:9092 172.20.233.31:9092 \
          10.10.250.103:40001 10.10.250.103:40002 10.10.250.103:40003 10.10.250.103:40004 10.10.250.103:40005 10.10.250.103:40006 \
          10.12.249.19:9092 172.20.233.32:9092; do
  port=$((19001 + n)); h=${ep%:*}; p=${ep#*:}
  iptables -t nat -C PREROUTING -p tcp --dport $port -j DNAT --to-destination $h:$p 2>/dev/null \
    || iptables -t nat -A PREROUTING -p tcp --dport $port -j DNAT --to-destination $h:$p
  n=$((n+1))
done
echo "dcim-relay(vm): $n 条转发规则就绪"

# /etc/systemd/system/dcim-relay.service
# [Unit]
# Description=DCIM Kafka relay (iptables forwarding to brokers)
# After=network-online.target
# Wants=network-online.target
#
# [Service]
# Type=oneshot
# RemainAfterExit=yes
# ExecStart=/usr/local/bin/dcim-relay.sh
#
# [Install]
# WantedBy=multi-user.target
