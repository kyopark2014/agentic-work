---
name: system-monitor
description: >
  시스템 현황 정보를 조회하고 보고하는 스킬입니다. 사용자가 "시스템 상태", "CPU 사용률",
  "메모리 현황", "디스크 용량", "서버 상태", "시스템 정보", "프로세스 목록", "리소스 사용량",
  "네트워크 상태", "업타임", "시스템 모니터링" 등을 요청할 때 반드시 이 스킬을 사용하세요.
  시스템 성능 문제, 리소스 부족 여부 확인, 서버 점검 등 모든 시스템 현황 관련 요청에 적극적으로 활용하세요.
---

# System Monitor Skill

사용자가 시스템 현황 정보를 요청하면, 아래 절차에 따라 Python 스크립트를 실행하여 결과를 수집하고 보기 좋게 정리해서 보여주세요.

## 수집할 정보 항목

1. **시스템 기본 정보**: OS, 호스트명, 커널 버전, 아키텍처, 업타임
2. **CPU**: 코어 수, 현재 사용률(%), 주파수
3. **메모리(RAM)**: 전체/사용/여유 용량, 사용률(%)
4. **디스크**: 마운트된 파티션별 전체/사용/여유 용량, 사용률(%)
5. **네트워크**: 인터페이스별 송수신 바이트
6. **상위 프로세스**: CPU 및 메모리 기준 상위 5개 프로세스

## 실행 방법

`execute_code` 도구를 사용하여 아래 Python 스크립트를 실행하세요.
`psutil`이 없으면 먼저 설치합니다.

```python
import subprocess, sys

# psutil 설치 확인
try:
    import psutil
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "psutil", "-q"])
    import psutil

import platform, datetime, socket

# ── 1. 시스템 기본 정보
uname = platform.uname()
boot_time = datetime.datetime.fromtimestamp(psutil.boot_time())
uptime = datetime.datetime.now() - boot_time
uptime_str = f"{int(uptime.total_seconds()//3600)}시간 {int((uptime.total_seconds()%3600)//60)}분"

print("=" * 55)
print("  🖥️  시스템 현황 리포트")
print("=" * 55)
print(f"  호스트명   : {socket.gethostname()}")
print(f"  OS         : {uname.system} {uname.release}")
print(f"  아키텍처   : {uname.machine}")
print(f"  부팅 시각  : {boot_time.strftime('%Y-%m-%d %H:%M:%S')}")
print(f"  업타임     : {uptime_str}")

# ── 2. CPU
cpu_percent = psutil.cpu_percent(interval=1)
cpu_count_logical = psutil.cpu_count(logical=True)
cpu_count_physical = psutil.cpu_count(logical=False)
try:
    cpu_freq = psutil.cpu_freq()
    freq_str = f"{cpu_freq.current:.0f} MHz" if cpu_freq else "N/A"
except Exception:
    freq_str = "N/A"

print("\n" + "─" * 55)
print("  🔲  CPU")
print("─" * 55)
print(f"  물리 코어  : {cpu_count_physical}개  /  논리 코어: {cpu_count_logical}개")
print(f"  현재 주파수: {freq_str}")
bar_cpu = "█" * int(cpu_percent // 5) + "░" * (20 - int(cpu_percent // 5))
print(f"  사용률     : [{bar_cpu}] {cpu_percent:.1f}%")

# ── 3. 메모리
mem = psutil.virtual_memory()
swap = psutil.swap_memory()

def to_gb(b): return b / (1024**3)

print("\n" + "─" * 55)
print("  💾  메모리 (RAM)")
print("─" * 55)
bar_mem = "█" * int(mem.percent // 5) + "░" * (20 - int(mem.percent // 5))
print(f"  전체       : {to_gb(mem.total):.2f} GB")
print(f"  사용 중    : {to_gb(mem.used):.2f} GB")
print(f"  여유       : {to_gb(mem.available):.2f} GB")
print(f"  사용률     : [{bar_mem}] {mem.percent:.1f}%")
print(f"  SWAP 사용  : {to_gb(swap.used):.2f} GB / {to_gb(swap.total):.2f} GB ({swap.percent:.1f}%)")

# ── 4. 디스크
print("\n" + "─" * 55)
print("  💿  디스크")
print("─" * 55)
partitions = psutil.disk_partitions()
for p in partitions:
    try:
        usage = psutil.disk_usage(p.mountpoint)
        bar_disk = "█" * int(usage.percent // 5) + "░" * (20 - int(usage.percent // 5))
        print(f"  {p.mountpoint:<12}: [{bar_disk}] {usage.percent:.1f}%")
        print(f"    전체 {to_gb(usage.total):.1f}GB  사용 {to_gb(usage.used):.1f}GB  여유 {to_gb(usage.free):.1f}GB")
    except PermissionError:
        print(f"  {p.mountpoint:<12}: 접근 권한 없음")

# ── 5. 네트워크
print("\n" + "─" * 55)
print("  🌐  네트워크")
print("─" * 55)
net_io = psutil.net_io_counters(pernic=True)
for iface, stats in net_io.items():
    if stats.bytes_sent == 0 and stats.bytes_recv == 0:
        continue
    def to_mb(b): return b / (1024**2)
    print(f"  {iface:<12}: ↑ {to_mb(stats.bytes_sent):.1f} MB  ↓ {to_mb(stats.bytes_recv):.1f} MB")

# ── 6. 상위 프로세스
print("\n" + "─" * 55)
print("  📋  상위 프로세스 (CPU 기준 Top 5)")
print("─" * 55)
procs = []
for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent']):
    try:
        procs.append(proc.info)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

top_cpu = sorted(procs, key=lambda x: x['cpu_percent'] or 0, reverse=True)[:5]
print(f"  {'PID':<8} {'이름':<25} {'CPU%':>6} {'MEM%':>6}")
print(f"  {'─'*8} {'─'*25} {'─'*6} {'─'*6}")
for p in top_cpu:
    print(f"  {p['pid']:<8} {(p['name'] or 'N/A')[:25]:<25} {p['cpu_percent'] or 0:>5.1f}% {p['memory_percent'] or 0:>5.1f}%")

print("\n" + "─" * 55)
print("  📋  상위 프로세스 (메모리 기준 Top 5)")
print("─" * 55)
top_mem = sorted(procs, key=lambda x: x['memory_percent'] or 0, reverse=True)[:5]
print(f"  {'PID':<8} {'이름':<25} {'CPU%':>6} {'MEM%':>6}")
print(f"  {'─'*8} {'─'*25} {'─'*6} {'─'*6}")
for p in top_mem:
    print(f"  {p['pid']:<8} {(p['name'] or 'N/A')[:25]:<25} {p['cpu_percent'] or 0:>5.1f}% {p['memory_percent'] or 0:>5.1f}%")

print("\n" + "=" * 55)
print("  ✅  조회 완료:", datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
print("=" * 55)
```

## 출력 형식

- 스크립트 실행 결과를 그대로 코드 블록으로 보여주세요.
- 결과 아래에 **요약 코멘트**를 한국어로 추가하세요:
  - 주의가 필요한 항목(사용률 80% 이상)은 ⚠️ 경고로 표시
  - 정상 범위면 ✅ 정상으로 표시
  - 필요 시 간단한 조치 방법 제안

## 주의사항

- 권한 문제로 일부 정보가 조회되지 않을 수 있으며, 이 경우 "접근 권한 없음"으로 표시합니다.
- 컨테이너/가상 환경에서는 일부 하드웨어 정보가 제한될 수 있습니다.
- 사용자가 특정 항목만 요청하면 해당 항목만 집중적으로 보여줘도 됩니다.
