#!/bin/bash
# ============================================================
# NAS Monitor 一键安装脚本（原生 systemd + venv 版）
# 飞牛 NAS → CH340 串口 → STM32 LCD 数据监控
# ============================================================
# 客户使用（在线）：
#   curl -fsSL https://你的地址/install.sh | SCRIPT_URL=https://.../send_data.py bash
#
# 客户使用（离线包）：
#   tar xzf nas-monitor-v1.0.tar.gz
#   sudo bash install.sh
#
# 离线识别：脚本自动检测同目录 offline/ 文件夹，
#   有则用本地 deb + whl，无则走 apt + pip
# ============================================================
set -euo pipefail

# ---------- 配置 ----------
INSTALL_DIR="/opt/nas-monitor"
VENV_DIR="$INSTALL_DIR/venv"
SERVICE_NAME="nas-monitor"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
SCRIPT_URL="${SCRIPT_URL:-}"  # send_data.py 的下载地址，留空则用本地同目录文件
LOG_TAG="[安装]"

# 脚本所在目录（离线包检测基准）
SCRIPT_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd || echo ".")"

# ---------- 离线模式自动检测 ----------
# 如果同目录有 offline/deb 和 offline/pip，就用本地依赖，不联网
OFFLINE_DEB_DIR=""
OFFLINE_PIP_DIR=""
if [ -d "$SCRIPT_DIR/offline/deb" ] && ls "$SCRIPT_DIR/offline/deb"/*.deb &>/dev/null; then
    OFFLINE_DEB_DIR="$SCRIPT_DIR/offline/deb"
fi
if [ -d "$SCRIPT_DIR/offline/pip" ] && ls "$SCRIPT_DIR/offline/pip"/*.whl &>/dev/null; then
    OFFLINE_PIP_DIR="$SCRIPT_DIR/offline/pip"
fi
if [ -n "$OFFLINE_DEB_DIR" ] || [ -n "$OFFLINE_PIP_DIR" ]; then
    INSTALL_MODE="离线"
else
    INSTALL_MODE="在线"
fi

# ---------- 颜色 ----------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}✅ $*${NC}"; }
warn()  { echo -e "${YELLOW}⚠️  $*${NC}"; }
fail()  { echo -e "${RED}❌ $*${NC}"; exit 1; }

# ---------- 权限检查 ----------
[ "$(id -u)" -eq 0 ] || fail "请用 root 运行：sudo bash install.sh"

echo "========================================"
echo "  NAS Monitor 一键安装 ($INSTALL_MODE模式)"
echo "========================================"

# ============================================================
# 步骤 1：检测 Python3
#   Python3 本体飞牛必然预装；离线包不含 python3 的 deb
#   （体量大且跨版本不兼容），只兜底尝试在线装
# ============================================================
echo "${LOG_TAG} [1/8] 检测 Python3 ..."
if ! command -v python3 &>/dev/null; then
    warn "未检测到 Python3，尝试安装 ..."
    apt-get update -qq && apt-get install -y -qq python3 python3-venv || fail "Python3 安装失败，请手动安装"
fi
PY_VER=$(python3 --version 2>&1)
info "Python3: $PY_VER"

# ============================================================
# 步骤 2：安装系统工具
#   脚本里 subprocess 调用的外部命令对应的 apt 包，全部检测：
#     ip route        → iproute2
#     ps              → procps
#     dmidecode       → dmidecode   (内存类型主路径)
#     lshw            → lshw        (内存类型 fallback，dmidecode 失败时兜底)
#     smartctl        → smartmontools (磁盘温度/健康)
#     sudo            → sudo        (脚本以 root 跑时透传，但需二进制存在)
#   df / hostname / systemctl 属 Debian 基础设施包，飞牛必然预装，不检测
# ============================================================
echo "${LOG_TAG} [2/8] 检测系统工具 ..."
NEED_INSTALL=()
command -v smartctl  &>/dev/null || NEED_INSTALL+=("smartmontools")
command -v dmidecode &>/dev/null || NEED_INSTALL+=("dmidecode")
command -v lshw      &>/dev/null || NEED_INSTALL+=("lshw")
command -v ip        &>/dev/null || NEED_INSTALL+=("iproute2")
command -v ps        &>/dev/null || NEED_INSTALL+=("procps")
command -v sudo      &>/dev/null || NEED_INSTALL+=("sudo")

if [ ${#NEED_INSTALL[@]} -eq 0 ]; then
    info "系统工具已全部就绪"
elif [ -n "$OFFLINE_DEB_DIR" ]; then
    # 离线模式：用本地 deb 包安装
    warn "缺少: ${NEED_INSTALL[*]}，使用离线 deb 包安装 ..."
    dpkg -i "$OFFLINE_DEB_DIR"/*.deb 2>/dev/null || {
        # dpkg 可能因依赖顺序失败，重试一次带 --force-depends
        warn "dpkg 首次安装有依赖警告，尝试强制安装 ..."
        dpkg -i --force-depends "$OFFLINE_DEB_DIR"/*.deb 2>/dev/null || true
        apt-get install -f -y -qq 2>/dev/null || true  # 修复依赖（在线时）
    }
    # 验证关键工具是否就位
    MISS_AFTER=""
    command -v smartctl  &>/dev/null || MISS_AFTER+="smartctl "
    command -v dmidecode &>/dev/null || MISS_AFTER+="dmidecode "
    command -v lshw      &>/dev/null || MISS_AFTER+="lshw "
    if [ -n "$MISS_AFTER" ]; then
        warn "离线安装后仍缺少: $MISS_AFTER（deb 版本可能不匹配，不影响核心功能）"
    else
        info "系统工具就绪（离线安装）"
    fi
else
    # 在线模式：apt 安装
    warn "缺少: ${NEED_INSTALL[*]}，正在在线安装 ..."
    apt-get update -qq && apt-get install -y -qq "${NEED_INSTALL[@]}" || fail "系统工具安装失败"
    info "系统工具就绪"
fi

# ============================================================
# 步骤 3：建立 venv 并安装 pyserial
#   venv 的核心作用：绕开 PEP 668（新 Debian 禁止 pip 装系统包）
# ============================================================
echo "${LOG_TAG} [3/8] 建立 Python 虚拟环境 ..."
mkdir -p "$INSTALL_DIR"
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR" || fail "venv 创建失败（可能缺少 python3-venv 包，请运行 apt install python3-venv）"
fi

if [ -n "$OFFLINE_PIP_DIR" ]; then
    # 离线模式：用本地 whl 安装，不联网
    "$VENV_DIR/bin/pip" install --no-index --find-links="$OFFLINE_PIP_DIR" pyserial -q \
        || fail "pyserial 离线安装失败"
    info "pyserial 已安装（离线 whl）"
else
    # 在线模式
    "$VENV_DIR/bin/pip" install --upgrade pip -q 2>/dev/null || true
    "$VENV_DIR/bin/pip" install pyserial -q || fail "pyserial 安装失败"
    info "pyserial 已安装（在线）"
fi
info "venv 就绪"

# ============================================================
# 步骤 4：部署 send_data.py
# ============================================================
echo "${LOG_TAG} [4/8] 部署数据采集脚本 ..."
if [ -n "$SCRIPT_URL" ]; then
    curl -fsSL "$SCRIPT_URL" -o "$INSTALL_DIR/send_data.py" || fail "脚本下载失败"
elif [ -f "$SCRIPT_DIR/send_data.py" ]; then
    cp "$SCRIPT_DIR/send_data.py" "$INSTALL_DIR/send_data.py"
else
    fail "未找到 send_data.py，请设置 SCRIPT_URL 环境变量或将其与本脚本放同目录"
fi
info "脚本已部署到 $INSTALL_DIR/send_data.py"

# ============================================================
# 步骤 5：自动扫描 CH340 串口
#   CH340 的 USB Vendor ID 是 1a86。ttyUSB 可能是 0/1/2...
# ============================================================
echo "${LOG_TAG} [5/8] 扫描 CH340 串口模块 ..."
SERIAL_PORT=""
for dev in /dev/ttyUSB*; do
    [ -e "$dev" ] || continue
    # 通过 udev 查 Vendor ID，1a86 = CH340/CH341
    vid=""
    if command -v udevadm &>/dev/null; then
        vid=$(udevadm info -q property -n "$dev" 2>/dev/null | grep -i '^ID_VENDOR_ID=' | cut -d= -f2)
    fi
    if [ "$vid" = "1a86" ]; then
        SERIAL_PORT="$dev"
        info "检测到 CH340 串口: $dev"
        break
    fi
done
# 降级：找不到 CH340，就用第一个 ttyUSB
if [ -z "$SERIAL_PORT" ]; then
    for dev in /dev/ttyUSB0 /dev/ttyUSB1 /dev/ttyUSB2 /dev/ttyUSB3; do
        if [ -e "$dev" ]; then
            SERIAL_PORT="$dev"
            warn "未识别到 CH340，降级使用: $dev"
            break
        fi
    done
fi
[ -n "$SERIAL_PORT" ] || fail "未找到任何 ttyUSB 设备，请检查 CH340 模块是否插入"

# 把扫描到的串口写进脚本
sed -i "s|^SERIAL_PORT = .*|SERIAL_PORT = '$SERIAL_PORT'|" "$INSTALL_DIR/send_data.py"
info "串口已配置: $SERIAL_PORT"

# ============================================================
# 步骤 6：配置 udev 规则，显式锁定 CH340 串口权限
#   不依赖 root：即使将来改成普通用户跑，或飞牛 udev 策略变化，
#   CH340 设备节点权限始终是 0666（内网 NAS 可接受）。
#   用 vendor ID 1a86 精确匹配 CH340/CH341，不误伤其他串口设备。
# ============================================================
echo "${LOG_TAG} [6/8] 配置串口权限 (udev 规则) ..."
UDEV_RULE="/etc/udev/rules.d/99-nas-monitor-ch340.rules"
cat > "$UDEV_RULE" <<EOF
# CH340/CH341 USB 转串口模块，飞牛 NAS Monitor 专用
# idVendor 1a86 = 江苏沁恒(CH340/CH341)
SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", MODE="0666", GROUP="dialout"
EOF
# 确保 dialout 组存在（Debian 默认有，兜底）
groupadd -f dialout &>/dev/null || true
# 重新加载 udev 规则并对现有设备立即生效（CH340 不用拔插重插）
udevadm control --reload-rules 2>/dev/null || true
udevadm trigger --action=add --subsystem-match=tty 2>/dev/null || true
info "udev 规则已写入: $UDEV_RULE (CH340 节点权限 0666)"

# ============================================================
# 步骤 7：生成 systemd service 并启动
# ============================================================
echo "${LOG_TAG} [7/8] 配置开机自启服务 ..."
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=NAS Monitor for STM32 LCD
After=network.target
# 确保串口设备就绪
After=dev-ttyUSB0.device dev-ttyUSB1.device

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
ExecStart=$VENV_DIR/bin/python -u $INSTALL_DIR/send_data.py
Restart=always
RestartSec=3
User=root
# 崩溃时日志标
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
info "service 文件已生成"

systemctl daemon-reload
systemctl enable "$SERVICE_NAME" &>/dev/null
systemctl restart "$SERVICE_NAME"
info "服务已启动并设为开机自启"

# ============================================================
# 步骤 8：健康检查
# ============================================================
echo "${LOG_TAG} [8/8] 健康检查 ..."
echo "    等待脚本产生数据（最多 15 秒）..."
HEALTH_OK=false
for i in $(seq 1 15); do
    sleep 1
    if journalctl -u "$SERVICE_NAME" --no-pager -n 5 2>/dev/null | grep -q "Sent JSON"; then
        HEALTH_OK=true
        break
    fi
done

echo ""
echo "========================================"
if [ "$HEALTH_OK" = true ]; then
    info "安装成功！数据已开始发送到屏幕"
    echo ""
    echo "  安装模式: $INSTALL_MODE"
    echo "  串口:     $SERIAL_PORT"
    echo "  脚本:     $INSTALL_DIR/send_data.py"
    echo "  虚拟环境: $VENV_DIR"
    echo "  udev规则: /etc/udev/rules.d/99-nas-monitor-ch340.rules"
    echo ""
    echo "  常用命令:"
    echo "    查看日志:   journalctl -u $SERVICE_NAME -f"
    echo "    重启服务:   systemctl restart $SERVICE_NAME"
    echo "    停止服务:   systemctl stop $SERVICE_NAME"
    echo "    卸载:       systemctl stop $SERVICE_NAME && systemctl disable $SERVICE_NAME && rm -rf $INSTALL_DIR $SERVICE_FILE /etc/udev/rules.d/99-nas-monitor-ch340.rules && udevadm control --reload-rules"
else
    warn "服务已启动但未检测到数据输出，请排查:"
    echo ""
    echo "  1. 查日志:       journalctl -u $SERVICE_NAME -n 30"
    echo "  2. 检查串口接线: CH340 RX→STM32 TX, CH340 TX→STM32 RX, GND 共地"
    echo "  3. 检查波特率:   脚本默认 9600，需与 STM32 一致"
    echo "  4. 确认 CH340:   ls -l $SERIAL_PORT (权限应为 crw-rw-rw- 或属 dialout 组)"
    echo "  5. 重载 udev:    udevadm control --reload-rules && udevadm trigger"
fi
echo "========================================"
