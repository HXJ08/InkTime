# InkTime 7C-photo 固件构建

适用于 **ESP32-S3 N8R8 + GDEM075F52 (480x800 4 色屏)** 墨水相框。

## 引脚对照

| 信号 | GPIO | 备注 |
|---|---|---|
| EPD BUSY | 9 |  |
| EPD RST  | 12 |  |
| EPD DC   | 11 |  |
| EPD CS   | 10 |  |
| EPD SCK  | 14 |  |
| EPD MOSI | 13 |  |
| BAT ADC  | 6  | 1S LiPo + 100k+100k 1:2 分压 |
| FACTORY RESET | 38 | 上电低电平触发 |
| LED | 2 | LED_BUILTIN |

## 电池分压电路

```
B+ ──┬── 100k ──┬── GPIO6
B- ──┘          │
                ├── 100k ── GND
                │
ESP32 GND ──────┘
```

## arduino-cli 构建命令

```bash
arduino-cli compile \
  --fqbn esp32:esp32:esp32s3 \
  --build-property "build.psram=opi" \
  --build-property "build.flash_size=16MB" \
  --build-property "build.partitions=min_spiffs" \
  --build-property "build.board=ESP32-S3-DevKitC-1" \
  --build-property "build.variant=esp32s3" \
  -u \
  esp32/ink-display-7C-photo
```

> ⚠️ `build.psram=opi` 必填，否则 framebuffer 分配失败 / init 死循环。
> ⚠️ N8R8 = 8MB OPI PSRAM，必须显式声明。
> ⚠️ `min_spiffs` 选这个分区方案以腾出更多 app 空间。

## GxEPD2 库

库版本 ≥ 1.5.x（需要 `GxEPD2_750c_GDEM075F52` 驱动 + `GxEPD2_4C` 模板）。

Arduino IDE 库管理器搜 "GxEPD2" 装最新即可。

## 画面布局（Apple Photos 风格信息面板）

渲染器（`render_daily_photo.py`）在照片底部 140px 区域画 5 行信息：

```
[相机型号(粗体)]                    [电池图标(ESP32 叠加)]
[镜头名称]
[分辨率 · 文件大小]
[ISO · 焦距 · 光圈 · 快门]
[日期]                    [地点]
```

- 电池图标位置：`battery_icons.h` 里 `BATTERY_ICON_X/Y`（440, 676），
  ESP32 在下载 .bin 后把它叠加到 framebuffer，渲染器留出该区域。
- 文字区高度 `TEXT_AREA_HEIGHT = 140`（y=660..800），渲染器与固件必须保持同步。

## 电池阈值（LiPo 9060100 8000mAh 1S）

| 等级 | 电池端 mV | 备注 |
|---|---|---|
| 100% | ≥ 4125 | 4.125V |
| 90%  | ≥ 3950 | 3.95V |
| 80%  | ≥ 3850 | 3.85V |
| 70%  | ≥ 3780 | 3.78V |
| 60%  | ≥ 3720 | 3.72V |
| 50%  | ≥ 3660 | 3.66V |
| 40%  | ≥ 3600 | 3.60V |
| 30%  | ≥ 3550 | 3.55V |
| 20%  | ≥ 3450 | 3.45V |
| 10%  | ≥ 3300 | 3.30V |
| 0%   | < 3300 | 3.00V 以下视为关机电压（代码里 3000mV 为下限） |

可在 `ink-display-7C-photo.ino` 顶部 `BATTERY_LEVEL_MV[]` 数组里按实际带载标定。

### 校准（解决 ADC 误差）

ADC eFuse 校准 + 电阻公差会让读出的电压有几 % 偏差。校准步骤：

1. 烧当前固件，串口 log 里看 `[BAT] adcMv=XXXX batMv=YYYY`（YYYY 是当前算出的电池电压）
2. 万用表量电池两端电压 V_actual
3. 算 `factor = V_actual / YYYY`
4. 改 `BATTERY_CALIBRATION_FACTOR` 常量，重刷

例：万用表 4.126V，串口 log 显示 `batMv=4050` → `factor = 4.126 / 4.050 = 1.019` → 改成 `1.019f`。

