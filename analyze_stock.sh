#!/usr/bin/env bash
# hikyuu 个股分析启动器：自动带上 LD_PRELOAD（解决 glibc TLS 空间不足导致的崩溃）
set -e
cd "$(dirname "$0")"
LD_PRELOAD=/lib/x86_64-linux-gnu/libgcc_s.so.1 \
    .venv/bin/python -m datacenter.analyze_stock "$@"
