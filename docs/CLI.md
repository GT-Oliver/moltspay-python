# MoltsPay CLI 命令参考

安装 Python 包后即可使用 `moltspay` 命令。所有命令默认将结果以格式化 JSON 输出；全局可使用 `--version` 查看版本。

```bash
pip install moltspay
moltspay --help
```

基础安装已包含 `qrcode`，因此 `fund` 命令可以直接在终端显示充值二维码：

```bash
moltspay fund 10 --chain base
```

如果使用的是旧版本或开发环境，可手动补装：

```bash
pip install "qrcode>=7.4"
```

> 注意：当前 `qrcode` 是基础依赖，不需要单独的 extra；上面的命令仅用于已有旧环境的手动补装，推荐直接重新安装或升级 `moltspay`。

## 命令总览

| 命令 | 用途 | 常用参数 / 位置参数 | 示例 |
|---|---|---|---|
| `init` | 创建 EVM 或 Solana 钱包 | `--chain`、`--config-dir`、`--force` | `moltspay init --chain base` |
| `status` | 查看钱包配置和余额 | `--chain`、`--all` | `moltspay status --chain base --all` |
| `faucet` | 从测试网水龙头领取代币 | `--chain`（默认 `base_sepolia`） | `moltspay faucet --chain base_sepolia` |
| `pay` | 发现并支付服务 | `url`、`service`、`--chain`、`--token`、`--rail`、`--prompt`、`--params`、`--timeout` | `moltspay pay https://moltspay.com/a/zen7 SERVICE_ID --prompt "a cat"` |
| `approve` | 批准 BNB 链代币支出 | 必填 `--chain`（`bnb` / `bnb_testnet`）、`--spender` | `moltspay approve --chain bnb_testnet` |
| `services` | 查询服务提供方发布的服务 | `url`、`--chain` | `moltspay services https://moltspay.com/a/zen7` |
| `fund` | 生成法币充值二维码 | `amount`、`--chain`（`base` / `polygon`） | `moltspay fund 10 --chain base` |
| `transfer` | 转账 USDC 或 USDT | `to`、`amount`、`--token`、`--chain` | `moltspay transfer 0x... 5 --token USDC --chain base` |
| `config` | 查看或更新客户端配置 | `--chain`、`--max-per-tx`、`--max-per-day`、`--rail-preference`、`--buyer-id` | `moltspay config --max-per-tx 10 --max-per-day 100` |
| `limits` | 查看或更新消费限额 | `--chain`、`--max-per-tx`、`--max-per-day` | `moltspay limits --max-per-tx 10 --max-per-day 100` |
| `balance` | 管理服务方余额账户 | 见下方子命令表 | `moltspay balance query https://provider.example` |
| `wechat` | 管理微信 Native 支付会话 | 见下方子命令表 | `moltspay wechat list` |
| `alipay` | 转发命令到官方 `alipay-bot` CLI | `action`、`args...` | `moltspay alipay --help` |

支持的链由当前版本的链配置决定，可通过命令帮助查看：

```bash
moltspay init --help
moltspay pay --help
```

## `pay` 参数

| 参数 | 说明 |
|---|---|
| `url` | 服务提供方地址 |
| `service` | 服务 ID |
| `--chain` | 支付链，默认 `base` |
| `--token` | 支付代币：`USDC` 或 `USDT`，默认 `USDC` |
| `--rail` | 支付通道：`balance`、`wechat` 或 `alipay` |
| `--prompt` | 传给服务的快捷 prompt 参数 |
| `--params` | JSON 格式的额外服务参数 |
| `--timeout` | 请求超时时间（秒），默认 `180` |
| `--buyer-id` | 余额账户买方 ID |
| `--topup-pack` | 自动充值时使用的充值套餐 |
| `--topup-mode` | 自动充值模式：`auto` 或 `manual` |
| `--no-auto-topup` | 禁用自动充值 |

`--prompt` 会被合并到 `--params` 中；如果同名，`--prompt` 的值会覆盖 JSON 中的值。

## `balance` 子命令

| 子命令 | 用途 | 参数 | 示例 |
|---|---|---|---|
| `query` | 查询买方余额 | `server` | `moltspay balance query https://provider.example` |
| `transactions` | 查询余额交易记录 | `server`、`--limit`（默认 20）、`--offset`（默认 0） | `moltspay balance transactions https://provider.example --limit 50` |
| `set-buyer` | 设置默认买方 ID | `id` | `moltspay balance set-buyer buyer-001` |
| `topup-order` | 创建充值订单 | `server`、`--pack`、`--buyer-id` | `moltspay balance topup-order https://provider.example --pack 10.00` |
| `topup-confirm` | 确认充值订单 | `id`、`--server` | `moltspay balance topup-confirm ORDER_ID --server https://provider.example` |
| `topup-status` | 查询充值订单状态 | `id` | `moltspay balance topup-status ORDER_ID` |
| `topup-list` | 列出本地充值会话 | 无 | `moltspay balance topup-list` |
| `topup-pack` | 直接执行余额充值套餐 | `server`、`--pack`、`--buyer-id` | `moltspay balance topup-pack https://provider.example --pack 10.00` |

## `wechat` 子命令

| 子命令 | 用途 | 参数 | 示例 |
|---|---|---|---|
| `start` | 创建微信支付会话并在终端显示二维码 | `server`、`service`、`--params` | `moltspay wechat start https://provider.example SERVICE_ID` |
| `status` | 查询支付会话状态 | `identifier` | `moltspay wechat status SESSION_ID` |
| `fulfill` | 完成支付会话 | `identifier` | `moltspay wechat fulfill SESSION_ID` |
| `cancel` | 取消支付会话 | `identifier` | `moltspay wechat cancel SESSION_ID` |
| `list` | 列出支付会话 | 无 | `moltspay wechat list` |

## 其他入口

| 命令 | 用途 |
|---|---|
| `moltspay-server` | 启动 MoltsPay 服务端 CLI；使用 `moltspay-server --help` 查看参数 |
| `moltspay-mcp` | 启动 MoltsPay MCP 服务；使用 `moltspay-mcp --help` 查看参数 |
