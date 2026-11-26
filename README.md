# SiHAS Canary

> **RCM/WCM fork maintained by brain-choi**
>
> Based on [cmsong-shina/sihas-canary](https://github.com/cmsong-shina/sihas-canary)
> v2.1.1, with four-channel RCM-300 local relay support and WCM-300-W local support.
> See [RCM protocol, installation, testing and migration notes](docs/rcm.md).
> See [WCM outlets, settings, power registers and verification status](docs/wcm.md).
> All four WCM power registers are verified against the app; their sum is a calculated total.
> WCM voltage is verified at register 60, with raw values divided by 10 for volts.
> WCM frequency uses the user-confirmed register 62 / 10 Hz mapping.
> WCM current uses the user-confirmed register 61 in mA, divided by 1000 for A.
> WCM device time uses register 63 to display the weekday and local time in Home Assistant.
> WCM settings include per-outlet button lock, overload and standby cutoff, and auto-off timers.
> Install a [published release](https://github.com/brain-choi/sihas-canary/releases)
> from this repository to retain the RCM changes across updates.


# 개요

[시하스 Wi-Fi 장치](https://sihas.co.kr/) to [HA](https://www.home-assistant.io/) 연동을 위한 컴포넌트입니다.

<br>



# 지원장치

[Wi-Fi 장치 연동 지원 현황](https://sihas.notion.site/db34d0e1209c4957899b0cc95ba26a4c)

<br>



# 설정

[HomeAssistant 설치 방법](https://sihas.notion.site/HomeAssistant-1385c75ce8b94e1aa812b70bb6e727c3?pvs=4)의 *HACS를 사용하여 설치* 항목을 참조해주세요.

> **NOTE**
>
> 문제가 있을 경우 같은 링크의 *직접 다운로드하여 설치*를 사용해주세요.  
> 해당 방법은 버전 컴포넌트 버전 [`v1.3.3`](https://github.com/cmsong-shina/sihas-canary/releases/tag/v1.3.3)까지 지원합니다.

<br>
