# openm454

[English](README.md) | 简体中文

[![CI](https://github.com/FrostFlops/openm454/actions/workflows/ci.yml/badge.svg)](https://github.com/FrostFlops/openm454/actions/workflows/ci.yml)

这是 **DreamSourceLab DSCope M454** USB 混合信号示波器(4 路模拟通道最高 400 MSa/s,8 路数字通道,内置 0-3 V 信号发生器)的开源驱动、命令行界面和 MCP 服务器。它的目标是让 AI 智能体(Claude Code 或任意 MCP 客户端)无需厂商图形界面,就能自己采集并分析真实信号。

M454 使用比开源 DSView 所支持设备更新的("v3")USB 协议。该协议是通过对 DSWave 1.2.1 做静态分析、注入命令驱动 DSWave 自身的硬件层以及 USB 抓包还原出来的,详见 `docs/PROTOCOL.md`。

## 使用

环境要求:Windows、Python 3.10+、示波器已绑定 WinUSB(默认即是),以及 DSWave 1.2.1(只需用一次,用来提取 FPGA 比特流,本项目绝不再分发它)。使用驱动前请关闭 DSWave:示波器只接受一个 USB 客户端。

```
pip install openm454            # or, from a clone: pip install -e .
```

把 `OPENM454_DSWAVE` 指向 DSWave 的 `DSWave.exe`(或其所在文件夹),或者把 DSWave 文件夹复制到 openm454 文件夹下的 `DSWave/`:克隆下来的仓库本身,或者对于 pip 安装的副本是 `%LOCALAPPDATA%\openm454`(其他系统为 `~/.local/share/openm454`;可用 `OPENM454_HOME` 覆盖)。该文件夹还存放缓存的比特流(`firmware/`)、状态(`state/`)和采集文件(`captures/`)。

```
python -m openm454 info
python -m openm454 capture -c A1,D0 --vdiv 1V --offset 1.5V --timebase 200us --trig A1:rise:1.4V
python -m openm454 watch -c A1 --every 0.5s --count 10 --fields A1.freq_hz,A1.vpp_v
python -m openm454 awg set --wave sine --freq 10k --low 0V --high 2V
python -m openm454 analyze captures/<file>.npz --window=-1ms:1ms
python -m openm454 capture -c D0,D1 --rate 20M --duration 2ms --trig D0:pulse>:200us --decode "i2c:scl=D0,sda=D1"
python -m openm454 selftest
```

## 记录模式与实测速度

| 命令 | 记录方式 | 长度 | 采样率 |
|---|---|---|---|
| `capture` | 写入 512 MiB 采样内存,之后再读出(250-265 MB/s) | 单条记录:每通道 400M 个采样点 / 512 MiB | 最高 400 MSa/s(单通道) |
| `stream` | 每条记录在采集的同时就发送出去,任何数据率均可(`--record-first`:数据率高于 102.4 MB/s 时先采集完再读出,与 DSWave 相同);超过一条记录的长度或 `--duration forever`:一条接一条地记录,每条刷新上一条 | 不限长度;读出能跟上时记录之间约 0.2 s,超过读出上限时再加上读出该记录剩余部分的时间(250 MB/s 时共 0.7 s;使用 `--record-first` 时 2.4 s);保留最后一条记录(`--save-each`:全部保留) | 与 `capture` 相同:模拟和数字各用各的采样率 |
| `log` | 循环模式:一条无间隙的无限数据流,保存在内存中(默认;结束时一次性写出;更长的运行由内存环形缓冲保留最新部分),或使用 `--storage disk`,运行时分块转储为文件 | 直到 `openm454 stop` 或达到指定时长;受内存预算 / 磁盘额度限制 | 所有通道同一采样率,100 MHz / N,N 为任意整数(1 kSa/s..100 MSa/s,例如 33.333 MSa/s);交错 200 MSa/s(<= 2 个模拟、<= 4 个数字通道)和 400 MSa/s(<= 1 个模拟、<= 2 个数字通道) |

在 M454 上通过 USB 3 实测,所有输入均接 1 kHz 方波,逐个周期检查是否有间隙(2026-11-01/02):

- 数据率为 采样率 x (模拟通道数 + 数字通道数 / 8) 字节/秒。
- `stream`:在 DSWave 的 100 MB/s 上限以及 200 MB/s 下均为实时。在 400 MB/s 时读出速度为 270-290 MB/s,其余数据留在采样内存中等待;记录完整到达。12 个通道、50 MSa/s(250 MB/s,约 2 s 的记录):边采集边发送(默认)时两条记录之间为 0.74 / 0.66 s,使用 `--record-first` 时为 2.35 / 2.45 s;记录完整且连续。
- `log`:150 MB/s(2 个模拟 + 8 个数字,50 MSa/s)运行 3 min(27 GB),以及写盘 30 s,主机延迟恒定;200 MB/s(2 个模拟,100 MSa/s)运行 20 s 无积压;5 MB/s 运行 10 min。在 250 MB/s(4 个模拟 + 8 个数字,50 MSa/s)时读出约 200 MB/s,积压以约 55 MB/s 增长。持续 3 s 的 300 MB/s 突发(2 个模拟 + 8 个数字,100 MSa/s)以 266 MB/s 读出,数据保持完整。
- `log` 在 100 MSa/s 以上时对转换器做交错,方式与 `capture` 完全相同(DSWave 在循环模式下从不这样做)。已在示波器上验证:1 个模拟通道 200 和 400 MSa/s、2 个模拟 200 MSa/s、2 个数字 400 MSa/s、4 个数字 200 MSa/s 以及混合通道组合。2 个数字通道 400 MSa/s 或 4 个数字通道 200 MSa/s 为 100 MB/s,可无时间限制地运行。1 个模拟通道 200 MSa/s(200 MB/s)运行 10 s 无积压。
- 循环模式下采样内存是一个 512 MiB 的环形缓冲:主机最多可落后约 512 MiB 而不丢数据(已吸收 50 MB/s 下 10 s 的主机停顿)。超过之后,示波器会在没有任何标志的情况下覆盖未读数据并继续输出数据流:此后的数据都是垃圾(模拟通道出现一次相位跳变,数字通道错位),直到重新启动运行。用驱动的积压估计值测得,在 508-514 MiB 处出现断裂(三次运行),因此 `log` 在 480 MiB 处停止。
- `log --plan`(无需设备)会输出针对某个采样率的结论、无间隙上限和建议(见下一节)、运行最长能持续多久(`max_duration_s`)以及数据去向:默认保存在内存(放不进预算时拒绝,除非加 `--keep-last`),`--storage disk` 写成约 256 MB 的分块文件(会警告 SSD 磨损:200 MB/s 相当于每小时 720 GB),`--storage none` 只做统计。
- `bench` 测量这台电脑(空闲内存、解码速度、磁盘写入速度、USB 读出速度),并记录用户允许录制和数据文件夹使用多少内存和磁盘(`bench --ram 8GB --disk 50GB --dir <folder>`,保存在 `state/host.json`);`log` 的默认值取自这里。本机实测:解码 468 MB/s,USB 读出 206 MB/s(4 个模拟 + 8 个数字)/ 267 MB/s(2 个模拟 + 8 个数字)。

### 超过读出上限时:`log` 如何判断

D = 数据率;R = 这台电脑在示波器采集时读取它的速度。R 取决于通道组合(本机实测:4 个模拟 + 8 个数字 50 MSa/s 为 202 MB/s,同样通道在 100 MSa/s 以及 2 个模拟 + 8 个数字 100 MSa/s 为 264 MB/s,4 个模拟 100 MSa/s 为 275 MB/s)。

1. R 是测出来的,而不是假定的:某个通道组合第一次在 150 MB/s 以上运行 `log` 时,会先做最长 2 s 的探测(输出一行 `probe`),并把 R 记入 `state/host.json`:实测速率,或者当读取速度 >= D 的 99 % 时记为"kept up"(R >= D,跟得上)。之后在 150 MB/s 以上运行时,若结果明确(停止时仍以稳定速率落后,或在结束时已追上)就会刷新它,`bench` 和 `--remeasure` 也一样。在此之前,`--plan` 假定为 190 MB/s,并把方案标为 `provisional`。
2. D <= R:主机跟得上(`sustained` / `near_limit`);运行时长只受存储空间限制。
3. D > R:示波器每秒领先 D - R(`backlog_growth_mb_s`),而且停止后它不再发送任何数据,所以未读部分会丢失。无间隙的记录最长为 `gapfree_max_s` = 480 MiB x R / (D x (D - R)),即示波器领先 480 MiB 时主机已读到的信号长度。更短的记录正常运行。
4. 更长的记录(或 `forever`)会与 `stream` 在相同通道和设置下的表现做比较(`stream_alternative`,来自 stream 自己的规划):每条记录最长 400M 个采样点每通道 / 512 MiB,边采集边发送,间隙为 记录时长 x (D - R) / R + 0.18 s,采用 stream 的采样率(1-2-5 步进 x 交错;某个域可能更慢,例如 2 个模拟 + 8 个数字 100 MSa/s 时数字通道按 50 MSa/s 采样):
   - `STREAM_BETTER`(退出码 2):stream 对每个通道的采样率都不低于 log,且它的记录至少和 log 那一段无间隙数据一样长(或者一条记录就能容纳整个时长)。错误信息中带有 stream 命令,垂直设置相同。
   - `NEEDS_FORCE`(退出码 2):log 的这一段是可能的最长无间隙记录,但比要求的短。`--force` 会录下这一段然后停止(`stop_reason` 为 `ring_full`,ok,带一条警告);请求时间的其余部分不会被记录。两个错误都在 `error.plan` 中带有具体数字。
5. 运行时,每条统计行都有 `backlog_mb`;积压增长时还有 `backlog_growth_mb_s`、`ring_full_in_s` 和 `gapfree_projected_s`。结果中有 `unread_at_stop_mb`(已采集但从未读取的部分)。

本机示例:12 个通道 50 MSa/s(250 MB/s,R = 204):无间隙 8.88 s,对比 stream 每条 2.03 s、间隙 0.64 s 的记录,所以是 `NEEDS_FORCE`;强制运行后在 8.84 s 停止(早先一次运行 R = 202 时为 8.30 s),直到最后一个采样点每个周期都在。12 个通道 100 MSa/s(500 MB/s,R = 264):1.13 s 对比 1.02 s 的记录、1.09 s 的间隙,仍是 `NEEDS_FORCE`;读出速度低于约 250 MB/s 时 `stream` 会更好。200 MB/s 以 190 MB/s 读出:积压增长 10 MB/s,约 50 s 后触发保护,得到 47.8 s 的无间隙信号。

积压的估计方法是 D x (距起点的时间) - 已接收字节数,在每次 USB 传输时检查,并对照一条跟随示波器 / 电脑时钟差的基线:基线每秒最多上升 200 ppm x D,而且只在主机已追上时(估计值低于 16 MiB)才上升,因此估计值在数小时内保持准确,真实的积压绝不会被吸收。保护停止时若完全没有数据到达,则报告为示波器停滞(`TIMEOUT_DATA`);计划恰好在保护点结束的强制 log 报告 `ring_full`,其他任何保护停止报告 `HOST_TOO_SLOW`。

### 用 `log` 还是 `stream`?

两者都是无触发记录,但在示波器上不是同一种模式。`log` 是一次无限运行(循环模式);`stream` 是一系列记录,每条都以固定长度编程,示波器在该长度处停止。

| | `log` | `stream` |
|---|---|---|
| 间隙 | 一次运行内没有 | 每条记录一个(刷新) |
| 两次运行之间的间隙,12 个通道 20 MSa/s(100 MB/s) | 0.18 s | 0.19 s |
| 两次运行之间的间隙,12 个通道 50 MSa/s(250 MB/s,超过读出上限) | 1.5 s 的运行之后 0.51 s:0.33 s 用来读出本次运行的积压(示波器此间采集的数据被丢弃),再加 0.18 s 重启 | 约 2 s 的记录之后 0.66-0.74 s:读出该记录的剩余部分(边采集边发送),然后重启;使用 `--record-first`(整条记录采集完后再整体读出)时为 2.33-2.45 s |
| 采样率 | 所有通道同一采样率:100 MHz / N,N 为任意整数,1-2 个通道可交错 200 / 400 MSa/s | 模拟和数字各用各的采样率(例如 1 个模拟 400 MSa/s 配 8 个数字 100 MSa/s),取自 DSWave 的 1-2-5 x 交错表(例如 80 MSa/s) |
| 超过读出上限(约 200-275 MB/s) | 一段最长 480 MiB x R / (D x (D - R)) 的无间隙数据(停止后示波器不再发送数据;见上文) | 每条最长 512 MiB / D(每通道 400M 个采样点)的记录,边采集边发送,有间隙 |
| 低于约 51 kB/s(例如 1 个通道 1-20 kSa/s) | 支持 | 采样率会被提高 |
| 协议解码器 | 对保存的文件离线解码(`analyze --decode`) | 对保留的记录使用 `--decode` |
| 寄存器程序 | DSWave 的 Data Acquisition 模式(交错:本驱动的扩展,已验证) | DSWave 针对该窗口自己的程序(已做 golden 测试) |

重启(约 0.18 s)对两者相同;区别在于重启之前必须读出的内容。需要连续记录时用 `log`;需要模拟和数字使用不同采样率、需要对记录解码,或者在超过读出上限且 `log` 提示 `STREAM_BETTER`、或者总时间比一段长的无间隙数据更重要时,用 `stream`。

```
python -m openm454 bench --ram 8GB --disk 50GB --dir logs
python -m openm454 log --plan -c A0,D0-D7 --rate 1M --duration 10min
python -m openm454 log -c A0,D0-D7 --rate 1M --duration 10min                   # kept in RAM, one .npz at the end
python -m openm454 log -c A0,D0-D7 --rate 1M --duration forever                 # RAM ring; end it with: python -m openm454 stop
python -m openm454 stream -c D0-D7 --rate 100M --duration 60s --every 1s
```

无论是否给出 `--json`,每条命令都输出一个 JSON 文档(`watch`、`stream` 和 `log`:每行一个,最后是结果):包括测量值、实际应用的设置、文件路径。采样点从不打印(除非用 `--dump N` 要求输出抽取后的序列);采集结果保存为 `.npz`(原始码值 + 比例系数)和 `.png`。`python -m openm454 <command> --help` 列出所有选项。Python API:`openm454.scope.Scope`、`openm454.capture.load_capture`、`Capture.decode()`。

协议解码器(`--decode SPEC`,可重复,用于 `capture`、`watch`、`stream` 和 `analyze`):`uart:rx=D4,baud=115200|auto,bits=8,parity=none,stop=1,invert=0`、`spi:clk=..,mosi=..,miso=..,cs=..,mode=0..3`、`i2c:scl=..,sda=..`、`can:rx=..,bitrate=500k|auto,invert=0`。它们会报告每一帧(时间相对于触发点)及其错误(校验/帧错误、不完整的字、NACK、CRC/填充/格式错误),并去除慢边沿上的比较器抖动。输出格式:`docs/schemas/decode.json`。解码需要每位或每个时钟周期约 10 个采样点(I2C:每个 SCL 周期 9 个,无错误;每周期 5-6 个仍可解码,错误远低于 1 %)。它们已用微控制器产生的真实总线做过验证(SPI 最高 18 MHz、RS232、CAN 最高 1 Mbit/s、I2C 最高 400 kHz),包括深存储采集和解码后的数据流。

## CLI 参考

`pip install openm454` 会安装 `openm454` 命令(`python -m openm454 ...` 等价)和 `openm454-mcp` 服务器。全局选项写在子命令之前:`--max-wall T`(看门狗:在 T 或根据命令计算出的预算之后停止并释放设备,取较长者;它绝不会缩短计算出的预算)、`--verbose`(在 stderr 输出线路日志)、`--json`(接受该选项,输出始终是 JSON)。
`python -m openm454 <command> --help` 列出所有选项。

| 命令 | 用途 | 主要选项 |
|---|---|---|
| `info` | 设备、固件、校准、限制 | |
| `capture` | 一次触发(或无触发)的记录:测量值、`.png`、`.npz` | 见下文 |
| `watch` | 重复采集,每次输出一行 JSON(监控用) | `capture` 的选项,`--every 0.5s`、`--count N`(未给出 `--count` 和 `--for` 时默认 20)、`--for T`、`--fields A1.freq_hz,...`、`--stop-when EXPR`、`--save-last` |
| `stream` | 边采集边发送的无触发记录,实时统计 | `capture` 的选项,`--every 1s`、`--save-each`、`--record-first` |
| `log` | 无间隙的无限记录(循环模式),所有通道同一采样率 | `-c`、`--rate 1M`、`--duration 10s\|forever`、`--storage ram\|disk\|none`、`--keep-last`、`--chunk`、`--ram`、`--plan`、`--force`、`--remeasure`、`--out`、`--tag`、`--awg`、垂直选项 |
| `bench` | 这台电脑的记录极限(空闲内存、解码和磁盘速度、USB 读出);保存内存 / 磁盘额度 | `--ram 8GB`、`--disk 50GB`、`--dir`、`--disk-test-mb`、`--no-device` |
| `awg set` | MCX 输出上的连续波形(0..3 V) | `--wave sine\|square\|triangle\|noise`、`--freq 1k`、`--low/--high` 或 `--vpp/--dc`、`--duty 50%`、`--phase` |
| `awg sweep` | 频率扫描,重复进行 | `--start`、`--end`(必需)、`--time 100ms`、`--log`、`--wave`、`--high`、`--low`、`--duty` |
| `awg burst` | 每个周期输出 N 个载波周期 | `--cycles`、`--period 10ms`、`--freq`、`--wave`、`--high`、`--low`、`--duty` |
| `awg mod` | 载波的 AM / FM / FSK 调制 | `--kind am\|fm\|fsk`、`--mod-wave`、`--mod-freq`、`--depth`、`--deviation`、`--hop`、`--freq`、`--wave`、`--high`、`--low`、`--duty` |
| `awg dc` | 稳定的直流电平(0..3 V,步进 6.4 mV) | `--level`(必需) |
| `awg off`、`awg status` | 把输出停放在 0 V / 显示上次的设置 | |
| `plot` | 把保存的 `.npz` 绘制成 PNG(无需设备) | `file`、`--window`、`--channels`、`--out` |
| `analyze` | 测量保存的 `.npz`(无需设备) | `file`、`--window`、`--channels`、`--edges N`、`--dump N`、`--decode SPEC` |
| `selftest` | 验证示波器(见[自检](#自检)) | `--quick`、`--no-awg`、`--fixture file\|standard` |
| `release` | 确保设备空闲并已释放 | |
| `recover` | 复位并重新加载挂起示波器的 FPGA,相当于重新上电(对寄存器读取无响应的示波器仍需重新插拔) | |
| `stop` | 请求正在运行的 `watch` / `stream` / `log`(例如在后台)干净地停止 | |
| `debug` | 维护用:`regs-read`、`regs-write`(同样的安全策略)、`plan`(打印某组 capture 选项对应的寄存器程序,不接触设备) | |

`capture`、`watch` 和 `stream` 共用这些选项:

- 通道 / 垂直:`-c A0-A3,D0-D7`(也可用 `analog`、`digital`)、`--vdiv 1V`、`--offset 0V`、`--coupling dc|ac`、`--bw20 A1,A2`、`--invert A2`、`--probe 10x|A2=10x`、`--threshold 1.4V`。按通道设置的值写作`A0=500mV,A2=2V`。
- 时基:`--timebase`(窗口 = 10 格)、`--duration`、`--samples`、`--rate`。
- 触发:`--trig SRC:EDGE[:LEVEL]`(例如 `A1:rise:1.4V`、`D0:fall`、`D0:pulse>:200us`)、`--trig-mode auto|normal|force`(默认 `auto`)、`--trig-pos 50%`、`--auto-after 0.3s`、`--timeout 5s`(normal 模式的等待时间)、`--holdoff`、`--sensitivity 0..15`(默认 5)。
- 输出:`--measure basic|full|none`、`--edges N`、`--dump N`、`--window t0:t1`、`--no-plot`、`--no-save`、`--out`、`--tag`、`--awg 'sine,10k,0V,2V[,duty%]'`(先设置信号发生器)、`--decode SPEC`(可重复)、`--decode-limit`(默认 50 帧)。

`log` 也接受垂直选项;它的时基是 `--rate` 和 `--duration`(见上面的记录章节)。
以 `-` 开头的值在 Python 3.10-3.12 上需要用 `=` 形式(`--window=-1ms:1ms`、`--offset=-1.5V`;按通道的 `A1=-1.5V` 保持原样即可);MCP 服务器会自行处理。
退出码:0 正常,1 其他(自检失败、内部错误、被中断),2 参数错误,3 未找到示波器,4 忙,5 未触发,6 设备 / 协议 / 看门狗故障,7
被安全规则拒绝(见错误一节)。

## 自检

`python -m openm454 selftest` 检查示波器本身。M454 没有内部测试信号、校准输出或回环通路:它唯一的信号源是信号发生器输出,而且厂商自己的自动校准甚至要求输入端悬空。因此检查分为两组。

无需接线的检查在任何接线下都能运行。它们只读取输入,绝不触碰信号发生器(约 4-6 s):

- V01 设备信息
- V12 对全部 12 个通道(A0-A3、D0-D7)以 100 MSa/s 做一次强制采集
- V13 最高采样率和折叠模式:1 个模拟通道 400 MSa/s、2 个模拟 200 MSa/s、2 个数字 400 MSa/s、4
  个数字 200 MSa/s
- V11 采集延迟

信号检查(V00、V02-V10:校准方波、V/div、采样率一致性、数字通道和阈值、触发位置、数字触发、无触发超时、信号发生器回环)需要下面的标准夹具,当接线没有声明为该夹具时,会标记为跳过并给出原因。它们的容差例如:1 kHz +-2 %,顶部 2.8 V +-12 %,底部 0 V +-0.15 V,占空比 50 +-3 %。

| 输入 | 标准夹具 |
|---|---|
| A0 | 示波器自己的信号发生器输出 |
| A1(1x 探头)、A2(10x 探头)、A3(1x 探头) | 外部约 2.8 V(0 到约 2.8 V)、1 kHz、50 % 占空比的方波 |
| D0、D2、D5、D7 | 同一个方波 |
| D1、D3、D4、D6 | 悬空 |

只需在数据文件夹的 `state/fixture.json` 中声明一次接线,或者用 `--fixture standard` 只对单次运行声明:

```
python -c "from openm454 import fixture; fixture.save('standard', 'standard')"
python -m openm454 selftest                      # uses the recorded wiring
python -m openm454 selftest --fixture standard   # the fixture is wired right now (this run only)
```

`fixture.save(analog='standard', digital='standard', note=None)` 写入 `{"analog": ..., "digital": ..., "since": ...}`;某一组引线的值为 `"standard"` 表示标准夹具(任何其他值表示接的是别的东西,这些引线上的信号检查会被跳过)。文件不存在表示接线未知,所以全新安装只运行无需接线的检查;文件无法读取时也是如此(结果中的 `note` 会说明原因;UTF-8 字节顺序标记没有问题)。接线变化时请更新该文件。

信号发生器只在模拟引线已声明为标准夹具且未给出 `--no-awg` 时使用:V09 把它停放在 0 V 以测试无触发超时,V10 向 A0 输出 2 Vpp 正弦波和 0-3 V 方波,然后关闭(其中某次采集失败时也会关闭)。`--no-awg`(以及 MCP 的默认设置)不触碰信号发生器;此时结果中不含 V10,而当最近一次 `awg` 设置让信号发生器仍在输出时,V09 会被跳过(先运行 `awg off` 即可纳入)。`--quick` 减少重复次数(时间约减半)。结果列出每项检查的 `measured` 和 `expected`;当有影响结论的检查失败时,退出码为 1。

## MCP 服务器

`openm454-mcp`(或 `python -m openm454.mcp_server`)通过 Model Context Protocol(stdio,无额外依赖;随 `pip install openm454` 一起安装)提供示波器服务:

```
claude mcp add openm454 -- openm454-mcp
```

如果 pip 的 `Scripts` 文件夹不在 `PATH` 中,请给出 `openm454-mcp` 的完整路径。其他客户端:
`{"mcpServers": {"openm454": {"command": "openm454-mcp"}}}`。每次工具调用在子进程中运行一条 CLI 命令,因此每次调用都会打开并释放示波器,挂起不会阻塞服务器,CLI 的所有规则都适用。

11 个工具:

| 工具 | 运行 | 主要参数(默认值) |
|---|---|---|
| `status` | `info` | 无。是否已连接?型号、USB 速度、固件 / FPGA 状态、上次的信号发生器设置 |
| `capture` | `capture` | `channels`(`A0-A3,D0-D7`)、`timebase` / `duration` / `rate` / `samples`、`trigger`(`SRC:EDGE[:LEVEL]`)、`trig_mode`(有触发时为 `normal`,无触发时为 `force`)、`timeout_s`(5)、`trig_pos`、`holdoff`、`decode`、`edges`、`window`、`save`(true)、`plot`(true)、垂直选项(`vdiv`、`offset`、`probe`、`coupling`、`bw20`、`threshold`)。每通道的频率、Vpp、最小/最大/平均值、占空比、上升/下降时间(数字通道:状态、频率、占空比、边沿数)、触发结果、解码帧、文件路径。未触发时返回 `TIMEOUT_NO_TRIGGER` |
| `analyze` | `analyze` | `file`(必需)、`channels`、`window`、`edges`、`decode`;对保存的 `.npz` 做测量 |
| `plot` | `plot` | `file`(必需)、`channels`、`window`、`out`;返回 `.png` 路径 |
| `awg` | `awg set` / `dc` / `off` / `status` | `action`(必需:`set`、`dc`、`off`、`status`);`wave`、`freq`、`high`、`low`(或 `vpp`、`dc`)、`duty`、`phase`;`dc` 用 `level`。调用结束后输出仍保持运行 |
| `log_plan` | `log --plan` | `rate`(必需)、`channels`、`duration`、`storage`、`keep_last`、`force`;无需设备 |
| `record_start` | 在后台运行 `log` 或 `stream` | `mode`(必需:`log` 或 `stream`)、`channels`、`rate`、`duration`、`every`、`storage`(log:`ram` / `disk` / `none`;stream:仅 `none`,不保存)、`keep_last` 和 `force`(仅 log)、`decode`(仅 stream);返回任务 id。同一时间只能有一个记录,期间示波器处于忙状态 |
| `record_status` | 无(读取任务的输出) | `job`(最近一个)、`lines`(3);最近的状态行,完成后给出结果 |
| `record_stop` | `stop` | `job`、`wait_s`(120);干净地停止并返回结果 |
| `release` | `release` | 无;释放被崩溃的程序遗留在运行状态的示波器 |
| `selftest` | `selftest` | `quick`(true)、`fixture`(`file`;或 `standard`)、`awg`(false)、`detail`、`max_wall_s`(1260) |

结果是摘要;对于 `capture`、`analyze` 和 `selftest`,`detail: "full"` 会返回 CLI 的完整文档。采样点保存在 `.npz` 中。每次设备调用都有硬性时限,超时后返回错误(`TIMEOUT_WATCHDOG`),并由 CLI 自己的看门狗停止示波器:`capture` 和 `selftest` 接受 `max_wall_s`(`capture` 的默认值:180 s 加触发超时和记录长度;`selftest`:1260 s,高于 CLI 自己的 1200 s 看门狗);`status`、`awg` 和 `release` 限制为 60 s。

`selftest` 运行 `selftest --quick`(当 `quick` 为 false 时去掉 `--quick`),除非 `awg` 为 true 否则带 `--no-awg`,当 `fixture` 为 `standard` 时带 `--fixture standard`,所以默认情况下它绝不触碰信号发生器,而且只有在接线已声明时才运行信号检查。只有在用户已接好并确认标准夹具之后才传 `fixture: "standard"`,只有在信号发生器输出接到 A0 且没有接其他任何东西时才传 `awg: true`。摘要包含 `passed_checks`、`failed_checks`(measured 对比 expected)、`skipped_checks`(按原因分组)以及与这些列表一致的计数(设备故障后未运行的检查计为跳过),另有 `fixture`、`note` 和 `seconds`。运行失败时返回错误 `SELFTEST_FAILED`,其提示会区分信号检查失败(通常是接线问题)和无需接线的检查失败(示波器或其 USB 连接有问题);设备故障时返回该设备错误(带 `stopped_at`)。

没有对应 MCP 工具的 CLI 命令:

| 命令 | 原因 |
|---|---|
| `watch` | 在多次采集中每次输出一行 JSON(默认 20 次,或直到 `--count` / `--for` / `--stop-when` / `openm454 stop`),不适合"一次请求一次响应"的模式;请使用 `mode: "stream"` 的 `record_start`,或重复调用 `capture` |
| `bench` | 维护:测量这台电脑的 USB 读出速度,`log` 会用到它 |
| `recover` | 复位并重新加载挂起示波器的 FPGA;有意保留为 CLI 手动步骤 |
| `debug` | 原始寄存器访问,有意不向智能体开放 |
| `stop` | `record_stop` 内部使用它 |
| `awg sweep`、`burst`、`mod` | `awg` 工具只覆盖 `set`、`dc`、`off`、`status` |

有 shell 的智能体可以用 CLI 完成以上所有操作。

## 错误

每个错误都有代码、信息和提示,因此下一步很明确:

| 代码 | 退出码 | 含义 |
|---|---|---|
| `NOT_FOUND` | 3 | 示波器未连接(没有看到 USB 2A0E:0080) |
| `BUSY` | 4 | DSWave 或另一个 openm454 进程占用着示波器 |
| `TIMEOUT_NO_TRIGGER` | 5 | 在 `--timeout` 内没有触发(`--trig-mode normal`) |
| `TRIGGER_OUT_OF_RANGE` | 2 | 触发电平超出该通道的可见范围(信息中会给出范围) |
| `INVALID_ARG` | 2 | 某个参数有误,或对该通道组合不可行 |
| `STREAM_BETTER`、`NEEDS_FORCE` | 2 | `log` 超过了它的无间隙上限(见上文) |
| `SELFTEST_FAILED` | 1 | 仅 MCP `selftest`(CLI 以 1 退出并列出各项检查):某项检查失败;`failed_checks` 给出 measured 对比 expected(信号检查失败通常意味着接线与标准夹具不同,无需接线的检查失败则指向示波器或其 USB 连接) |
| `UNSAFE_OPERATION` | 7 | 被安全规则拒绝 |
| `TIMEOUT_WATCHDOG`、`TIMEOUT_DATA`、`USB_ERROR`、`PROTOCOL_ERROR`、`HOST_TOO_SLOW` | 6 | 运行期间设备或主机出错;此前的数据会保留 |

## 目录结构

- `openm454/` 驱动:带安全白名单的 USB 传输层、设备启动(FPGA、安全握手、校准)、寄存器编译器(`mso.py`,精确复现 DSWave 的寄存器程序)、数据流解码器(`packet.py`)、采集引擎、循环模式记录器(`logger.py`)、测量、绘图、AWG、CLI、自检、协议解码器(`decode.py`)、测试用的合成总线流量(`protosim.py`)以及 MCP 服务器(`mcp_server.py`)。
- `tests/` 离线测试:golden 寄存器和 AWG 程序、数据流解码器、针对模拟设备的采集引擎、测量、基于合成流量的协议解码器、MCP 服务器、安全规则(`python -m pytest tests`)。
- `docs/PROTOCOL.md` 还原出的 USB 协议。
- `.github/workflows/`:CI(在 Windows 和 Linux、Python 3.10-3.14 上运行离线测试,使用 `OPENM454_NO_USB=1`,让驱动拒绝打开 USB;检查受版本控制的文件不含本地路径;构建包),在推送到 main 和提交 pull request 时触发;与 `openm454.__version__`相同的标签 `vX.Y.Z` 会运行测试、构建并发布到 PyPI(trusted publishing),同时创建 GitHubrelease。

## 此处不包含的内容

厂商应用程序、其内嵌的 FPGA 比特流和固件镜像、反编译的厂商代码、原始 USB 抓包(其中含有设备的安全密钥)、逆向工程工作区以及厂商数据手册,均不放在本仓库中。

## 安全

驱动绝不发送固件升级请求(bRequest 0xB3),绝不写入设备 EEPROM,只有在设备报告 FPGA 未配置时,或者当 `openm454 recover` 复位挂起的示波器时,才会配置 FPGA(始终使用经过验证的比特流,并且是会话中的第一次写入)。

## 许可证

GPL-3.0(见 `LICENSE`)。DreamSourceLab 的 DSView(GPLv3)被用作协议参考。DSWave 和 DSCope 是 DreamSourceLab 的商标;本项目与 DreamSourceLab 无关联。
