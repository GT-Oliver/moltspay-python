# MoltsPay Python SDK 测试报告

## 1. 测试概况

| 项目 | 内容 |
|---|---|
| 项目 | MoltsPay Python SDK |
| 版本 | `2.4.0` |
| 测试日期 | 2026-08-05 |
| 工作目录 | `D:\WorkPlace\GitHub\moltspay-python` |
| Python 环境 | `.venv\Scripts\python.exe` |
| 单元测试框架 | `pytest` |
| CLI 参考记录 | [CLI-RUN-RESULTS.md](CLI-RUN-RESULTS.md) |

本报告汇总自动化测试和 CLI 实际运行测试。CLI 测试涉及本地服务、测试网账户、真实钱包余额及第三方支付状态，因此部分失败或未完成结果属于环境限制，不直接判定为代码缺陷。

## 2. 测试范围

自动化测试覆盖以下模块：

- Client API 与服务调用
- 钱包创建、配置和安全特性
- 余额查询与充值
- x402 支付流程
- Fiat 支付客户端
- CLI 命令及参数处理

CLI 实际运行测试覆盖：

- 钱包初始化、状态和多链余额查询
- 服务发现与余额支付
- faucet、approve、transfer 等链上操作
- Coinbase 充值链接和终端二维码
- 消费限额配置与查询
- 本地余额查询、交易记录和充值订单
- 微信支付会话创建、查询、完成、取消和列表
- `moltspay-server` 及主要帮助命令

## 3. 自动化测试结果

### 3.1 执行命令

```powershell
& .\.venv\Scripts\python.exe -m pytest -q
```

### 3.2 执行结果

```text
52 passed, 1 warning in 21.03s
```

| 指标 | 结果 |
|---|---:|
| 测试用例总数 | 52 |
| 通过 | 52 |
| 失败 | 0 |
| 跳过 | 0 |
| 通过率 | 100% |
| 执行耗时 | 21.03 秒 |

### 3.3 测试文件

| 测试文件 | 覆盖内容 |
|---|---|
| `tests/test_client.py` | 客户端核心行为 |
| `tests/test_wallet.py` | 钱包相关功能 |
| `tests/test_security_features.py` | 安全特性 |
| `tests/test_fiat_clients.py` | 法币支付客户端 |
| `tests/test_x402.py` | x402 支付流程 |
| `tests/test_balance.py` | 余额查询与管理 |
| `tests/test_balance_topup.py` | 余额充值流程 |
| `tests/test_cli.py` | CLI 命令行为 |

### 3.4 警告

测试过程中出现 1 条依赖警告：`websockets.legacy` 已被弃用。该警告来自当前虚拟环境中的依赖，不影响本次测试通过结果。后续可在依赖升级时一并处理。

## 4. CLI 实际运行结果

CLI 结果复用 [CLI-RUN-RESULTS.md](CLI-RUN-RESULTS.md) 的 2026-08-05 实际运行记录，共 30 项。

| 分类 | 数量 | 说明 |
|---|---:|---|
| 成功完成 | 26 | 命令返回退出码 `0` |
| 受外部状态影响的失败 | 3 | faucet 限额、链上 nonce、USDC 余额不足 |
| 未完成 | 1 | 微信支付未实际完成，流程持续等待支付确认 |
| 合计 | 30 | — |

### 4.1 成功验证的关键能力

- `moltspay --help`、各主要子命令 `--help` 正常运行。
- `moltspay init --chain base --force` 成功创建/初始化钱包。
- `moltspay status --chain base --all` 成功返回 8 条链的余额信息。
- `moltspay pay ... --rail balance` 成功调用本地服务，`ping` 返回 `{"ok": true}`。
- `moltspay services https://moltspay.com/a/zen7` 成功发现 2 个服务。
- `moltspay fund 10 --chain base` 成功生成充值链接和终端二维码。
- `config` 和 `limits` 成功设置并读取每笔 `10`、每日 `100` 的限额。
- 本地余额查询、交易记录、充值订单创建和充值状态查询成功。
- 微信会话的创建、状态查询、完成、取消和列表操作成功。
- `moltspay-server --help` 正常运行。

### 4.2 失败或未完成项说明

| 操作 | 结果 | 原因 | 判断 |
|---|---|---|---|
| `faucet --chain base_sepolia` | 退出码 `1` | 水龙头 24 小时限领 | 环境/账户状态限制 |
| `approve --chain bnb_testnet` | 退出码 `1` | 链上 nonce 太低 | 链上账户状态限制 |
| `transfer ... --token USDC --chain base` | 退出码 `1` | USDC 余额不足 | 钱包余额限制 |
| `balance topup-pack ...` | 未完成 | 需要实际完成微信支付后才能结束轮询 | 外部支付未完成 |

上述结果与 [CLI-RUN-RESULTS.md](CLI-RUN-RESULTS.md) 中的记录一致。充值订单确认接口能够正确返回 `pending=true`、`credited=false`，说明未完成支付被识别为待处理状态，而不是被误判为成功。

## 5. 风险与后续建议

1. 升级或替换使用 `websockets.legacy` 的依赖，消除弃用警告。
2. 为链上操作准备独立的测试钱包，确保 nonce、原生代币和 USDC 余额满足测试条件。
3. 为 faucet 增加可控的 mock 或固定测试账户，避免 24 小时领取限制影响回归测试。
4. 为微信支付轮询增加可注入的支付完成 mock，覆盖 `credited`、`pending`、`cancelled` 和超时分支。
5. 如需发布质量门禁，可增加覆盖率检查：

   ```powershell
   & .\.venv\Scripts\python.exe -m pytest --cov=src/moltspay --cov-report=term-missing
   ```

## 6. 测试结论

自动化测试全部通过：52/52，未发现单元测试层面的功能性失败。CLI 实际运行中，核心命令和本地支付/充值/会话管理流程均已验证成功；4 项非成功结果均可由 faucet 限额、链上账户状态、余额不足或未完成第三方支付解释。

基于本次测试结果，当前版本可以进入后续发布检查；若发布范围包含完整链上转账和真实微信支付闭环，建议先完成相应的专用测试环境验证。
