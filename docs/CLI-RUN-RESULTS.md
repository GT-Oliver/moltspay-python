# MoltsPay CLI 实际运行记录

> 最新重跑结果（2026-08-05）：针对本地服务 `http://127.0.0.1:8402/` 的失败任务已完成复测。`pay`、余额查询、充值订单创建、微信会话管理和 `fund` 均已成功；faucet 限额、链上 nonce、USDC 余额不足及未完成微信支付导致的结果保持失败或 pending。详细命令和输出见文末“失败任务重跑”章节。

- 执行日期：2026-08-05
- 工作目录：`D:\WorkPlace\GitHub\moltspay-python`
- Python：`.venv\Scripts\python.exe`
- MoltsPay：`2.4.0`
- 参考文档：[CLI.md](CLI.md)
- 说明：命令按 CLI.md 中的出现顺序执行。为避免占位符支付命令等待过久，`pay` 示例额外使用了 `--timeout 5`；其余参数保持文档示例。

## 执行汇总

| 编号 | 命令 | 退出码 | 结果 |
|---:|---|---:|---|
| 01 | `pip install moltspay` | 0 | 已安装，当前环境已是 `2.4.0` |
| 02 | `moltspay --help` | 0 | 成功，列出 13 个顶层命令 |
| 03 | `moltspay init --chain base --force` | 0 | 成功，钱包地址为 `0xea595b4459593e9Fe76fFad3C47e5B77C04a5348` |
| 04 | `moltspay status --chain base --all` | 0 | 成功，返回 8 条链的余额信息 |
| 05 | `moltspay faucet --chain base_sepolia` | 1 | 失败：水龙头 24 小时限领 |
| 06 | `moltspay pay http://127.0.0.1:8402 ping --rail balance --timeout 5` | 0 | 成功，`ping` 返回 `{"ok": true}`，扣除 0.01 CNY |
| 07 | `moltspay approve --chain bnb_testnet` | 1 | 失败：链上 nonce 太低 |
| 08 | `moltspay services https://moltspay.com/a/zen7` | 0 | 成功，发现 2 个服务 |
| 09 | `moltspay fund 10 --chain base` | 0 | 成功生成 Coinbase USDC/Base 充值链接和终端二维码 |
| 10 | 使用真实钱包地址执行 `moltspay transfer ... 0.01 --token USDC --chain base` | 1 | 参数有效，但余额不足：`Insufficient USDC balance` |
| 11 | `moltspay config --max-per-tx 10 --max-per-day 100` | 0 | 成功，配置为每笔 10、每日 100 |
| 12 | `moltspay limits --max-per-tx 10 --max-per-day 100` | 0 | 成功，返回限额及今日已消费金额 |
| 13 | `moltspay balance query http://127.0.0.1:8402` | 0 | 成功，余额 `84.00 CNY` |
| 14 | `moltspay balance transactions http://127.0.0.1:8402 --limit 50` | 0 | 成功返回余额交易记录 |
| 15 | `moltspay balance set-buyer buyer-001` | 0 | 成功，随后已恢复为 `local-buyer` |
| 16 | `moltspay balance topup-order http://127.0.0.1:8402 --pack 20.00` | 0 | 成功创建订单 `WX526c7ca50897a09540066163240f16` 并返回二维码链接 |
| 17 | 使用上述订单执行 `balance topup-confirm` | 0 | 成功查询；因微信订单未支付，返回 `pending=true`、`credited=false` |
| 18 | 使用上述订单执行 `balance topup-status` | 0 | 成功返回 `pending` 状态 |
| 19 | `moltspay balance topup-list` | 0 | 成功，返回 2 条本地充值会话 |
| 20 | `moltspay balance topup-pack http://127.0.0.1:8402 --pack 20.00` | - | 未完成：该流程需要实际完成微信支付后才能结束自动轮询 |
| 21 | `moltspay wechat start http://127.0.0.1:8402 ping` | 0 | 成功创建本地微信会话并生成二维码 |
| 22 | 使用实际会话 ID执行 `wechat status` | 0 | 成功返回会话状态（过期会话为 `expired`） |
| 23 | 使用实际会话 ID执行 `wechat fulfill` | 0 | 成功处理本地过期会话，状态为 `cancelled` |
| 24 | 使用实际会话 ID执行 `wechat cancel` | 0 | 成功，状态为 `cancelled` |
| 25 | `moltspay wechat list` | 0 | 成功返回本地微信会话列表 |
| 26 | `moltspay alipay --help` | 0 | 成功，显示 `action` 和 `args` 参数 |
| 27 | `moltspay init --help` | 0 | 成功，显示链、配置目录和强制覆盖参数 |
| 28 | `moltspay pay --help` | 0 | 成功，显示支付、通道、充值和超时参数 |
| 29 | `moltspay-server --help` | 0 | 成功，显示技能目录、端口和主机参数 |

## 关键输出

### `status --chain base --all`

命令成功，钱包地址为 `0xea595b4459593e9Fe76fFad3C47e5B77C04a5348`。检测到的余额摘要：

| 链 | USDC | USDT | 原生代币 |
|---|---:|---:|---:|
| `bnb` | 0 | 0 | 0 |
| `bnb_testnet` | 2.0 | 0 | 0.002 |
| `base` | 0 | 0 | 0 |
| `base_sepolia` | 3.96 | 3.96 | 0 |
| `polygon` | 0 | 0 | 0 |
| `tempo_moderato` | 0 | 0 | 输出显示异常大的原生余额：`4.242424242424243e+57` |
| `solana` | 0 | 0 | SOL 为 0 |
| `solana_devnet` | 0 | 0 | SOL 为 0 |

当前配置为：`maxPerTx=10.0`、`maxPerDay=100.0`、`buyerId=local-buyer`。

### `services https://moltspay.com/a/zen7`

命令成功，服务提供方为 `Zen7 Video`，支持 `base`、`base_sepolia`、`polygon`、`tempo_moderato`、`solana_devnet`、`bnb` 和 `bnb_testnet`。

发现的服务：

| 服务 ID | 名称 | 价格 | 必需参数 |
|---|---|---:|---|
| `b23c6959-605f-49ff-98de-aea28705d386` | Text to Video | 0.01 USDC | `prompt` |
| `091f5602-a483-4a37-8778-78bfb0e0d599` | Image to Video | 1.49 USDC | `image` |

### `config`、`limits`

两条命令均成功：

```json
{
  "max_per_tx": 10.0,
  "max_per_day": 100.0,
  "spent_today": 0.0
}
```

### `balance topup-list`

命令成功，返回 2 条已有本地充值会话：1 条 `credited`、1 条 `pending`。本次测试没有创建新的充值订单。
