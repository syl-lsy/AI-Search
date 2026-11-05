"""预算/硬超时/取消三类 guard + deadline-aware 重试预算感知（占位锚点，B3 实装）。

规划（ARCH §3.1 / §6.2）：guard 是图级可靠性基元——预算控制、硬超时、取消响应三件套；
图级 RetryPolicy 是唯一重试所有者（网关侧 num_retries=0 去重）。
"""
