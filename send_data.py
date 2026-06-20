import serial
import termios
import time
import json
import subprocess
import os
import shutil
import re
import glob

SERIAL_PORT = '/dev/ttyUSB0'
BAUD_RATE = 9600
RECONNECT_INTERVAL = 2
UART_SETTLE_DELAY = 0.1

last_rx_bytes = 0
last_tx_bytes = 0
last_net_time = time.time()
last_cpu_total, last_cpu_idle = 0, 0

last_hardware_fetch_time = 0
hardware_cache_interval = 30
cached_cpu_model = "N/A"
cached_mem_type = "N/A"

_cached_interface = None

def auto_detect_interface():
    global _cached_interface
    if _cached_interface is not None:
        return _cached_interface
    iface = _get_default_route_interface()
    if iface:
        _cached_interface = iface
        print(f"[网络] 通过默认路由检测到网卡: {iface}")
        return iface
    iface = _get_busiest_physical_interface()
    if iface:
        _cached_interface = iface
        print(f"[网络] 通过流量检测到网卡: {iface}")
        return iface
    iface = _match_fallback_interface()
    if iface:
        _cached_interface = iface
        print(f"[网络] 通过降级列表检测到网卡: {iface}")
        return iface

def _get_default_route_interface():
    try:
        with open('/proc/net/route', 'r') as f:
            for line in f.readlines():
                fields = line.strip().split()
                if len(fields) >= 2 and fields[1] == '00000000':
                    iface = fields[0]
                    if iface and iface != 'lo':
                        return iface
    except Exception:
        pass
    try:
        result = subprocess.run(['ip', 'route', 'show', 'default'], capture_output=True, text=True, timeout=2)
        if result.returncode == 0 and result.stdout.strip():
            parts = result.stdout.split()
            if 'dev' in parts:
                idx = parts.index('dev')
                if idx + 1 < len(parts):
                    return parts[idx + 1]
    except Exception:
        pass
    return None

def _get_busiest_physical_interface():
    VIRTUAL_PREFIXES = ('lo', 'docker', 'br-', 'veth', 'virbr', 'vmnet', 'flannel', 'cni', 'cali', 'tun', 'tap', 'wg')
    try:
        with open('/proc/net/dev', 'r') as f:
            lines = f.readlines()[2:]
        candidates = []
        for line in lines:
            name = line.split(':')[0].strip()
            if any(name.startswith(p) for p in VIRTUAL_PREFIXES):
                continue
            data = line.split(':')[1].split()
            rx = int(data[0]) if data else 0
            tx = int(data[8]) if len(data) > 8 else 0
            total = rx + tx
            if total > 0:
                candidates.append((name, total))
        if candidates:
            candidates.sort(key=lambda x: x[1], reverse=True)
            return candidates[0][0]
    except Exception:
        pass
    return None

def _match_fallback_interface():
    FALLBACK_PATTERNS = ['eth0', 'eth1', 'ens33', 'ens160', 'ens192', 'ens256', 'eno1', 'eno2', 'enp3s0', 'enp0s3', 'enp0s25', 'enp2s0', 'en0']
    try:
        with open('/proc/net/dev', 'r') as f:
            existing = [line.split(':')[0].strip() for line in f.readlines()[2:]]
        for pattern in FALLBACK_PATTERNS:
            if pattern in existing:
                return pattern
        VIRTUAL_PREFIXES = ('lo', 'docker', 'br-', 'veth', 'virbr', 'vmnet', 'flannel', 'cni', 'cali', 'tun', 'tap', 'wg')
        for name in existing:
            if not any(name.startswith(p) for p in VIRTUAL_PREFIXES):
                return name
    except Exception:
        pass
    return None

def get_process_count():
    try:
        proc_count = 0
        run_count = 0
        for entry in os.listdir('/proc'):
            if entry.isdigit():
                proc_count += 1
                try:
                    with open(f'/proc/{entry}/stat', 'r') as f:
                        stat_data = f.read().split()
                        if len(stat_data) > 2:
                            if stat_data[2] == 'R' or stat_data[2] == 'S':
                                run_count += 1
                except:
                    pass
        return proc_count, run_count
    except Exception as e:
        print(f"获取进程数失败: {e}")
        return 0, 0

def get_cpu_model():
    try:
        with open('/proc/cpuinfo', 'r') as f:
            cpuinfo = f.read()
        for line in cpuinfo.split('\n'):
            if 'model name' in line.lower():
                model = line.split(':')[1].strip()
                if '@' in model:
                    model = model.split('@')[0].strip()
                model = model.replace('(R)', '').replace('(TM)', '').replace('CPU', '').strip()
                if len(model) > 20:
                    match = re.search(r'(i[0-9]-[0-9a-zA-Z]+|Ryzen [0-9] [0-9a-zA-Z]+)', model)
                    if match:
                        model = match.group(0)
                    else:
                        model = model[:20]
                return model
    except Exception as e:
        print(f"获取CPU型号失败: {e}")
    return "N/A"

def get_memory_type():
    try:
        result = subprocess.run(['sudo', 'dmidecode', '-t', 'memory'], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            for line in result.stdout.split('\n'):
                if 'Type:' in line and 'Unknown' not in line:
                    mem_type = line.split('Type:')[1].strip()
                    if mem_type and mem_type != '<OUT OF SPEC>':
                        return mem_type
    except Exception as e:
        print(f"通过dmidecode获取内存类型失败: {e}")
    try:
        result = subprocess.run(['lshw', '-short', '-C', 'memory'], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            for line in result.stdout.split('\n'):
                if 'memory' in line and 'DDR' in line.upper():
                    match = re.search(r'DDR[0-9]', line.upper())
                    if match:
                        return match.group(0)
    except Exception as e:
        print(f"通过lshw获取内存类型失败: {e}")
    return "N/A"

def get_partition_info(max_partitions=3):
    partitions = []
    try:
        result = subprocess.run(['df', '-h'], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            lines = result.stdout.strip().split('\n')
            for line in lines[1:]:
                parts = re.split(r'\s+', line)
                if len(parts) >= 6:
                    mount_point = parts[5]
                    if (mount_point in ['/', '/home', '/data', '/mnt', '/media', '/var', '/opt'] or
                        mount_point.startswith('/mnt/') or mount_point.startswith('/media/') or
                        mount_point.startswith('/data')):
                        try:
                            percent = float(parts[4].replace('%', ''))
                        except:
                            percent = 0.0
                        partitions.append({'mount': mount_point, 'total': parts[1], 'used': parts[2], 'available': parts[3], 'percent': percent})
        partitions.sort(key=lambda x: x['percent'], reverse=True)
        return partitions[:max_partitions]
    except Exception as e:
        print(f"获取分区信息失败: {e}")
    return [{'mount': '/', 'total': '0G', 'used': '0G', 'available': '0G', 'percent': 0.0}]

def get_root_partition_info():
    try:
        result = subprocess.run(['df', '-h', '/'], capture_output=True, text=True, timeout=2)
        if result.returncode == 0:
            lines = result.stdout.strip().split('\n')
            if len(lines) > 1:
                parts = re.split(r'\s+', lines[1])
                if len(parts) >= 6:
                    return {'mount': parts[5], 'total': parts[1], 'used': parts[2], 'available': parts[3], 'percent': float(parts[4].replace('%', ''))}
    except Exception as e:
        print(f"获取根分区信息失败: {e}")
    return {'mount': '/', 'total': '0G', 'used': '0G', 'available': '0G', 'percent': 0.0}

def get_disk_temperature(disk_device='/dev/sda'):
    try:
        result = subprocess.run(['sudo', 'smartctl', '-A', disk_device], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            for line in result.stdout.split('\n'):
                if 'Temperature_Celsius' in line or 'Temperature' in line:
                    parts = line.split()
                    if len(parts) >= 10:
                        for part in reversed(parts):
                            if part.isdigit():
                                return int(part)
        temp_paths = [f'/sys/class/block/{os.path.basename(disk_device)}/device/hwmon/hwmon*/temp1_input', f'/sys/class/block/{os.path.basename(disk_device)}/device/temp1_input', '/sys/class/hwmon/hwmon*/temp1_input']
        for pattern in temp_paths:
            for path in glob.glob(pattern):
                try:
                    with open(path, 'r') as f:
                        return int(f.read().strip()) // 1000
                except:
                    continue
    except Exception as e:
        print(f"获取磁盘温度失败: {e}")
    return 0

def get_disk_health(disk_device='/dev/sda'):
    try:
        result = subprocess.run(['sudo', 'smartctl', '-H', disk_device], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            output = result.stdout.lower()
            if 'passed' in output:
                return 1
            elif 'failed' in output:
                return 0
        result = subprocess.run(['sudo', 'smartctl', '-A', disk_device], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            for line in result.stdout.split('\n'):
                if 'Reallocated_Sector_Ct' in line or 'Reallocated_Sector_Count' in line:
                    parts = line.split()
                    if len(parts) >= 10:
                        raw_value = parts[9]
                        if raw_value.isdigit() and int(raw_value) > 0:
                            return 0
    except Exception as e:
        print(f"获取磁盘健康状态失败: {e}")
    return 1

def get_disk_device():
    try:
        result = subprocess.run(['df', '/'], capture_output=True, text=True)
        if result.returncode == 0:
            for line in result.stdout.split('\n'):
                if '/' in line:
                    parts = line.split()
                    if len(parts) > 0:
                        device = parts[0]
                        if device.startswith('/dev/'):
                            if device[-1].isdigit():
                                return device.rstrip('0123456789')
                            return device
        for device in ['/dev/sda', '/dev/nvme0n1', '/dev/mmcblk0']:
            if os.path.exists(device):
                return device
    except:
        pass
    return '/dev/sda'

def get_cpu_usage():
    with open('/proc/stat', 'r') as f:
        lines = f.readlines()
    for line in lines:
        if line.startswith('cpu '):
            parts = line.split()
            total_time = sum(int(x) for x in parts[1:])
            idle_time = int(parts[4])
            return total_time, idle_time
    return 0, 0

def get_memory_usage():
    with open('/proc/meminfo', 'r') as f:
        lines = f.readlines()
    mem_info = {}
    for line in lines:
        if 'MemTotal:' in line:
            mem_info['total'] = int(line.split()[1])
        elif 'MemAvailable:' in line:
            mem_info['available'] = int(line.split()[1])
    if mem_info['total'] > 0:
        used = mem_info['total'] - mem_info['available']
        return round((used / mem_info['total']) * 100, 2)
    return 0.0

def get_ip_address():
    try:
        result = subprocess.run(['hostname', '-I'], capture_output=True, text=True, timeout=1)
        if result.returncode == 0 and result.stdout.strip():
            all_ips = result.stdout.strip().split()
            if all_ips:
                return all_ips[0]
    except:
        pass
    return "N/A"

def get_disk_usage():
    try:
        total, used, free = shutil.disk_usage("/")
        return round((used / total) * 100, 2)
    except Exception as e:
        print(f"通过shutil获取磁盘使用率失败: {e}")
    return 0.0

def get_network_speed(interface=None):
    global last_rx_bytes, last_tx_bytes, last_net_time
    if interface is None:
        interface = auto_detect_interface()
    current_time = time.time()
    time_diff = current_time - last_net_time
    if time_diff < 0.1:
        time_diff = 0.1
    try:
        with open('/proc/net/dev', 'r') as f:
            lines = f.readlines()
    except:
        return 0.0, 0.0
    rx_bytes = 0
    tx_bytes = 0
    for line in lines:
        if interface in line:
            data = line.split()
            if len(data) >= 10:
                rx_bytes = int(data[1])
                tx_bytes = int(data[9])
            break
    if last_rx_bytes == 0 and last_tx_bytes == 0:
        rx_speed = 0.0
        tx_speed = 0.0
    else:
        rx_speed = (rx_bytes - last_rx_bytes) / time_diff
        tx_speed = (tx_bytes - last_tx_bytes) / time_diff
    last_rx_bytes = rx_bytes
    last_tx_bytes = tx_bytes
    last_net_time = current_time
    return round(tx_speed / 1024, 2), round(rx_speed / 1024, 2)

def get_service_status(service_name):
    try:
        result = subprocess.run(['systemctl', 'is-active', service_name], capture_output=True, text=True, timeout=2)
        return result.stdout.strip() == 'active'
    except:
        return False

def cleanup_serial_lock():
    lock_path = '/var/lock/LCK..ttyUSB0'
    try:
        if os.path.exists(lock_path):
            os.remove(lock_path)
            print(f"[串口] 已清理锁文件: {lock_path}")
    except Exception as e:
        print(f"[串口] 清理锁文件失败: {e}")

def flush_serial_input(s):
    try:
        time.sleep(UART_SETTLE_DELAY)
        import fcntl
        fd = s.fileno()
        termios.tcflush(fd, termios.TCIFLUSH)
    except Exception:
        pass
    try:
        s.reset_input_buffer()
    except Exception:
        pass

def open_serial():
    try:
        if not os.path.exists(SERIAL_PORT):
            return None
        cleanup_serial_lock()
        s = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
        flush_serial_input(s)
        print(f"[串口] 已连接: {SERIAL_PORT} @ {BAUD_RATE}")
        return s
    except serial.SerialException as e:
        print(f"[串口] 打开失败: {e}")
        return None

def wait_for_serial():
    print(f"[串口] 等待设备 {SERIAL_PORT} ...")
    attempt = 0
    while True:
        attempt += 1
        s = open_serial()
        if s is not None:
            return s
        if attempt % 5 == 1:
            print(f"[串口] 仍在等待 {SERIAL_PORT} ... (已等待约{attempt * RECONNECT_INTERVAL}秒)")
        time.sleep(RECONNECT_INTERVAL)

def close_serial(s):
    if s is not None:
        try:
            s.close()
        except Exception:
            pass

last_total, last_idle = get_cpu_usage()
time.sleep(0.1)
disk_device = get_disk_device()
print(f"检测到磁盘设备: {disk_device}")
network_iface = auto_detect_interface()
print(f"检测到网络接口: {network_iface}")
ser = wait_for_serial()

try:
    partition_index = 0
    while True:
        loop_start_time = time.time()
        current_total, current_idle = get_cpu_usage()
        cpu_percent = 0.0
        if current_total > last_total:
            total_delta = current_total - last_total
            idle_delta = current_idle - last_idle
            cpu_used = total_delta - idle_delta
            cpu_percent = round((cpu_used / total_delta) * 100, 2) if total_delta > 0 else 0
        last_total, last_idle = current_total, current_idle
        memory_percent = get_memory_usage()
        ip_address = get_ip_address()
        disk_percent = get_disk_usage()
        tx_speed, rx_speed = get_network_speed()
        smb_status = get_service_status('smbd') or get_service_status('smb')
        nfs_status = get_service_status('nfs-server') or get_service_status('nfs')
        ssh_status = get_service_status('ssh') or get_service_status('sshd')
        disk_temp = get_disk_temperature(disk_device)
        disk_health = get_disk_health(disk_device)
        all_partitions = get_partition_info(max_partitions=3)
        if not all_partitions:
            all_partitions = [get_root_partition_info()]
        current_partition = all_partitions[partition_index % len(all_partitions)]
        partition_index += 1
        proc_total, proc_running = get_process_count()
        current_time = time.time()
        if current_time - last_hardware_fetch_time >= hardware_cache_interval:
            cached_cpu_model = get_cpu_model()
            cached_mem_type = get_memory_type()
            last_hardware_fetch_time = current_time
            print(f"硬件信息已更新: CPU={cached_cpu_model}, RAM={cached_mem_type}")
        cpu_model = cached_cpu_model
        mem_type = cached_mem_type
        system_data = {
            "cpu": cpu_percent, "mem": memory_percent, "ip": ip_address,
            "disk": disk_percent, "tx": tx_speed, "rx": rx_speed,
            "smb": 1 if smb_status else 0, "nfs": 1 if nfs_status else 0, "ssh": 1 if ssh_status else 0,
            "temp": disk_temp, "health": disk_health,
            "part_mount": current_partition['mount'], "part_total": current_partition['total'],
            "part_used": current_partition['used'], "part_available": current_partition['available'],
            "part_percent": current_partition['percent'],
            "proc_total": proc_total, "proc_run": proc_running,
            "cpu_model": cpu_model, "mem_type": mem_type
        }
        json_data_str = json.dumps(system_data) + '\n'
        try:
            ser.write(json_data_str.encode('utf-8'))
            ser.flush()
            print(f"Sent JSON: {json_data_str.strip()}")
        except (OSError, serial.SerialException, termios.error) as e:
            print(f"[串口] 写入失败: {e}")
            close_serial(ser)
            print("[串口] 设备可能已断开，尝试重连...")
            ser = wait_for_serial()
            try:
                ser.write(json_data_str.encode('utf-8'))
                ser.flush()
                print(f"[串口] 重连后补发成功: {json_data_str.strip()}")
            except Exception as e2:
                print(f"[串口] 重连后补发失败: {e2}，下次循环重试")
        loop_elapsed_time = time.time() - loop_start_time
        sleep_time = 5.0 - loop_elapsed_time
        if sleep_time > 0:
            time.sleep(sleep_time)
except KeyboardInterrupt:
    print("程序终止")
except Exception as e:
    print(f"程序运行出错: {e}")
finally:
    if ser is not None:
        ser.close()
