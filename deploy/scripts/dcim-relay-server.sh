#!/bin/bash
# DCIM Kafka 中转（**我方服务器侧**，装在 rz-ubuntu-1）
#
# 配合虚拟机侧的 dcim-relay-vm.sh：把「发往各园区 broker 的包」改道到中转 VM 的
# 19001~19013 端口，由 VM 再转给真正的 broker。
#
# 为什么能解决 advertised 问题：改道发生在**我方**的 nat 表里，客户端的 socket
# 始终以为自己连的是 `10.4.0.60:9092`；broker 返回的节点表（advertised）正是这些
# 地址，客户端按表再连时同样被改道 —— 全程对 Kafka 客户端透明，不需要改任何配置。
#
# 两条链都要写：
#   PREROUTING —— 容器（worker 跑在 docker 里）发出的包走这条
#   OUTPUT     —— 主机自身发出的包（如手工 nc 测试）走这条
#
# 安装（已在 rz-ubuntu-1 上执行过）：
#   install -m 755 dcim-relay-server.sh /usr/local/bin/dcim-relay.sh
#   systemctl enable --now dcim-relay.service（unit 见文件末尾注释）
#
# 端口映射必须与虚拟机侧那份脚本**逐条一致**（序号即端口偏移）。

set -u

RELAY=10.10.250.89   # 中转虚拟机

n=0
for ep in 10.4.0.60:9092 10.12.251.18:9092 10.12.250.18:9092 172.20.222.46:9092 172.20.233.31:9092 \
          10.10.250.103:40001 10.10.250.103:40002 10.10.250.103:40003 10.10.250.103:40004 10.10.250.103:40005 10.10.250.103:40006 \
          10.12.249.19:9092 172.20.233.32:9092; do
  port=$((19001 + n)); h=${ep%:*}; p=${ep#*:}
  for ch in PREROUTING OUTPUT; do
    iptables -t nat -C $ch -d $h -p tcp --dport $p -j DNAT --to-destination $RELAY:$port 2>/dev/null \
      || iptables -t nat -I $ch 1 -d $h -p tcp --dport $p -j DNAT --to-destination $RELAY:$port
  done
  n=$((n+1))
done
echo "dcim-relay(server): $n 个 broker 端点已改道到 $RELAY"

# /etc/systemd/system/dcim-relay.service
# [Unit]
# Description=DCIM Kafka relay (redirect broker IPs to relay VM)
# After=network-online.target docker.service
# Wants=network-online.target
#
# [Service]
# Type=oneshot
# RemainAfterExit=yes
# ExecStart=/usr/local/bin/dcim-relay.sh
#
# [Install]
# WantedBy=multi-user.target
#
# 排查：
#   iptables -t nat -S PREROUTING | grep 10.10.250.89   # 期望 13 条
#   iptables -t nat -S OUTPUT     | grep 10.10.250.89   # 期望 13 条
#   conntrack -L -d 10.4.0.60                           # 看改道后的会话
