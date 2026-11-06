# GetDuoNow 产品规格 v1（定稿 2026-11-01）

这一版以 mvp 版为底，加入 robust 版的监控、同意证据和上线门槛，再加入 growth 版的测试流程和"抢到那一刻推荐"，并修掉两轮评审指出的问题。代码已对照 ~/duo-monitor 的 HEAD 4fc0f09 复核：
- features/{admin, extend, followup, optout, referral, stories}.py 和测试**都已提交**（4fc0f09，已推到 origin/main）。`.venv/bin/python -m pytest -q` 结果是 118 passed。这些 feature 已经覆盖：
  - events 日志、续期
  - STOP/HELP 收件：Mac 中转读 chat.db，加 Twilio webhook `/twilio/inbound`（optout.py:197）
  - 推荐
  - 成功故事：/stories 已经公开上线

  本规格在它们的基础上改，不重写。
- 已经做好、本规格不用再做的：
  - 购买链接按 part 直达（messages.py:14）
  - FAMILIES 可配（config.py:22）
  - 前端先选系列、再选组合，默认是主推款（index.html:136/156-160/224，web.py:95-104）
  - 运营商默认 unlocked（web.py:136、index.html:127）
  - webhook 校验 payment_status=paid（web.py:281）
- notify.py 有四处没真正送达却返回成功：
  - outbox：63-66
  - 拉取队列：67-70，只是入队
  - console 短信：71-72
  - console 邮件：91-92
- 现有行为里和本规格冲突、需要改的：
  - STOP 会把这个号码下所有订阅 expire（optout.py:45-49、148-150）。
  - /unsub 和 List-Unsubscribe 也会把整个订阅 expire（optout.py:52-53、122-127）。
  - Y/YES 被归在 START 集合里（optout.py:29）。
  - Notifier.alert 给短信和邮件用同一段正文（notify.py:28-36，web.py:219），所以退订链接、/got 链接、推荐链接这些 footer 也全进了短信。
  - 线上已经公开承诺"0 提醒全额退"（terms.html:45、68，index.html:106）。

## 0. 一句话定位
美国 Apple Store 门店自提库存的付费提醒。每 30 秒检测一次，有货就发短信和邮件，用户自己去 Apple 下单。只提醒，不代下单，所有档的提醒一样快。Not affiliated with Apple.

Phase 1 的 SLO 达标之前，对外文案保持现在的 "Checked every 30 sec"，不写"几秒内送达"，避免 FTC 虚假宣传风险。

## 1. 12 个问题

1. **收费**
   - 现在：一次性 $4.99 买 14 天，走 SoleScout 的 Stripe，账单显示 SOLESCOUT.AI。线上公开承诺"整个监控期一次都没提醒就全额退"（terms.html:45、68；index.html:106 "No restock? Full refund"）。
   - 定稿：
     - 继续卖一次性 Pass，不自动续费。另加 Pass+ $9.99/30 天。
     - 收费前开独立 Stripe 账户，账单名 GETDUONOW。
     - 退款政策改成 §3 的新版，同时改上面三处文案。改动之前已售出的订单，仍按旧承诺全额退。
     - 月订阅等复购率 ≥20%，或 ≥10 人主动要，再上。
2. **推荐**
   - 现在：`/?r=CODE` 会把码存进 localStorage，再写进 checkout metadata。但有三个问题：
     - 门禁开着时，`/` 要 key 或 cookie 才能进，朋友点链接直接 401（web.py:43-56）。
     - localStorage 里的码不会过期（index.html:129）。
     - 码按 sub_id 存（referral.py:27-31），续期一次就多一个码。
   - 定稿：
     - 每人（按 email）一个推荐码，分享 `/r/CODE`。这是公开路由，会种一个 30 天的 cookie。
     - 门禁期另外给每人 3 个邀请码 `/i/CODE`，邀请码本身带归因。
     - 朋友付款后，双方各 +7 天。推荐人当场到账；没有在用的监控，就存进 credit，下一单自动用。
     - 全额退款或拒付，就把奖励收回。
     - 有效推荐数决定 L1–L3（§4）。
3. **实时库存**
   - 现在：
     - web 进程里有一个线程，每轮**按顺序**请求 Apple pickup-message 接口。
     - 每个请求都新建一个 httpx.Client（apple.py:92，poller.py:34-41），约 0.6–1.0 秒一个。
     - 提醒在同一个线程里同步发出（poller.py:55-59），Twilio 和 Resend 的超时各 20 秒。
     - 轮询间隔 = max(1, 30 − 本轮耗时)（poller.py:74）。
     - Mac 中转每 5 秒拉一次短信队列（sms_relay.py:97）。
     - 店少的时候还能 30 秒一轮；到 50 店 × 40 part，一轮要 150–250 秒。
   - 改完后（本周 ⑤）：
     - 只查有人在盯的（店, part）组合，每店每 8 个 part 一个请求。
     - 共用连接，8 并发，一轮约 10 秒。
     - 抓完在内存里比对，用一个短写事务落库，提交后再发通知。从无货变有货就发。
     - Phase 2 再把发送挪进独立队列（§6）。
4. **默认无锁**：已经是默认（web.py:136、index.html:127）。
   - 自提库存按硬件 part 算，不分运营商，cppart 参数无效。
   - 下拉框保留，旁边写 "Pickup stock is the same for every carrier; you pick your carrier at Apple checkout."
   - 链接保持 /shop/product/<part> 直达，不改成系列页，抢货时少选一步。
5. **每个用户的数据**
   - 身份键：email（已经必填）加 E.164 格式的手机号。
   - 行为写进只追加的 events 表。表里加一列 email_hash，用来按人汇总。指标用 SQL 现算。
   - 你在 /admin/user?email= 看（已有）。
   - 用户在管理页 `/m/<token>` 看自己的：
     - token 按 email 签名，页面列出这个 email 名下的所有监控。续期产生的新 sub 也在里面，旧链接不会失效。
     - 页面上还有一条自己的事件时间线：付款、提醒、送达、点击、续期、推荐、退订。
6. **服务商**
   - 短信：Twilio toll-free 号码，做 TFV，不走 10DLC。
   - 邮件：Resend（已接好）。Gmail 进垃圾箱就换 Postmark。
7. **回 Y / 退订测试**
   - 先在 ①b 把退订改成按渠道处理，否则测试流程跑不通：
     - STOP 只停短信，不 expire 订阅。
     - Y 不能把 stopped 改回来，只有 START/UNSTOP 能恢复。
   - TFV 之前：用 pytest 伪造带签名的 inbound 请求。
   - TFV 之后：用自己手机完整走一遍（§5.4）。
   - toll-free 号码上的 STOP 由运营商自动处理，回复内容固定，我们只负责同步状态。
8. **取消/升级**
   - 管理链接是 `/m/<token>`，按 email 签名，续期后照样能用。放在付款邮件里，不用密码。
   - 停提醒分三种：
     - 管理页的 "Stop alerts"：停整个监控（短信加邮件）。
     - 短信回 STOP：只停短信。
     - 邮件一键退订：只停邮件。
   - 后两种要新加渠道级状态（subscriptions.email_off + suppression），列在本周 ①b。
   - Pass 升 Pass+：补 $5，到期日 = 原到期日 + 16 天。
   - 续期：用已有的 /extend 链接，天数接在原到期日后面。
9–10. **档位**：Free（只能来自赠送或礼券）、Pass、Pass+，推荐等级叠加在上面，见 §2。
11. **成功故事**
    - 现在：
      - /stories 和 /api/stories/summary 已经上线，不在门禁后面（web.py:43-44，stories.py:224-257）。
      - 只展示"抢到了并同意展示"的（got=1 且 consent=1，stories.py:96）。
      - 顶部数字是用户自报的"抢到"数（stories.py:106-107）。
    - 定稿：
      - 本周 ⑪ 改两处：也展示同意展示的"没抢到"；顶部数字改成从 cell.soldout 和 alerts 算（抓到几次补货、卖完时间中位数）。用户自报的抢到数单独标注。
      - 回访：提醒邮件里的 /got 链接已经有了，Phase 2 加短信回 1/2/3。
      - 用户说抢到后，单独征得展示同意，再给分享页。分享页带推荐码，并披露奖励（§8）。
12. **日志**
    - 不用 Render 日志（只留 7 天），暂时也不用 Render Postgres。
    - 存在 /var/data 上 SQLite 的 events 表里，开 WAL。
    - 每天有 Render 快照，每晚再做一份异地备份（§7）。

## 2. 服务等级

| | Free（Comp / 礼券） | Pass | Pass+ |
|---|---|---|---|
| 机型系列 | 1 | 1（默认 iPhone Duo） | 3 |
| 门店 | 3 | 3 | 8 |
| 容量×颜色 | 所选系列内不限 | 同左 | 同左 |
| 时长 | Comp 由 admin 定；礼券 7 天 | 14 天 | 30 天 |
| 渠道 | 邮件；回 Y 后加短信 | 同左 | 同左 |
| 价格 | $0，不算有效推荐 | $4.99 | $9.99（Pass 升级补 $5，+16 天） |

**推荐解锁**（叠加在当前档上）：

| 等级 | 有效推荐数 | 解锁 |
|---|---|---|
| L1 | 1 | 机型系列 +1 |
| L2 | 3 | 门店 +3 |
| L3 | 6 | 送一次 30 天 Pass+，加 "Founding member" 标 |

**硬上限**：不管哪一档、哪一级，都不超过 3 个系列、8 家店。超出上限的奖励折成 +7 天。这条用来防黄牛，也控制打 Apple 的请求量。

**发送规则**：
- 每人每天最多 10 条短信（含回访），超出的只发邮件。
- 同一家店同一轮有多个组合补货，合并成 1 条。
- 所有档速度一样。每次补货的发送顺序随机打乱，不让任何人永远排最后。

**不设常驻免费档**：
- 只收邮件、又排在后面的免费用户，几乎注定抢不到，反而伤口碑。
- 还会马上撞上 Resend 免费档每天 100 封的上限。
- 请求量不受影响：一个系列有几种容量×颜色（未核实），但每店请求数 = ceil(part 数/8)，有上界。

## 3. 收费与取消、升级

**Checkout**：
- `mode=payment`，`managed_payments.enabled=false`。
- 先按邮箱查找或新建 Customer。"首单"判断依赖它，防止自己推荐自己。
- `metadata={sub_id, kind: pass|upgrade|extend, ref}`。

**Webhook**：
- `checkout.session.completed`：
  - payment_status=paid 校验已有（web.py:281），按 kind 分别处理。
  - 卡 fingerprint 从 `PaymentIntent.latest_charge.payment_method_details.card.fingerprint` 取（Customer 对象上没有），存进 subscriptions.card_fingerprint，推荐去重用。
- `charge.refunded`：只有 `amount_refunded == amount`（全额退款）时，才停提醒、收回推荐奖励。部分退款（比如补偿 $1）只记一条 refund 事件，不停服务。
- `charge.dispute.created`：立刻停提醒，收回推荐奖励。
- `checkout.session.expired`：清掉没付款的 pending 记录。
- 每个事件的 `event.id` 写进 events.idem 去重。

**升级、续期、取消**：
- **升级**：
  - 到期日 = 原到期日 + 16 天（30 − 14）。
  - 例子：Pass 还剩 21 天（含推荐送的 7 天），升级后剩 37 天，推荐的天数原样保留。
  - 不提供降级。
- **续期**：沿用 extend.py。新监控的到期日接在旧监控后面，旧 sub 被 expire。管理链接按 email 签名，所以续期后不失效。
- **取消**：
  - 管理页的 "Stop alerts"：对这个 email 名下所有 active 的 sub 调 db.expire，短信和邮件都停。
  - 短信回 STOP：只停短信，订阅保持 active，邮件照发。
  - 邮件一键退订：只给这个订阅设 email_off，短信照发。
  - 后两种在本周 ①b 实现，现在的代码是整单 expire。
  - Pass 不自动续费，不会出现"取消了还扣钱"。

**退款**（结账页和 /terms 用同一段话）：
- 付款 24 小时内、还没发过提醒：全额退。Phase 1 你在 Stripe 后台手动退，Phase 2 做成管理页自助。
- 整个监控期 0 条提醒：自动免费延长一次，天数和原来一样。还是没有，用户申请就退。
- 已经发过提醒：不退。
- 文案要同步改：
  - terms.html:45（Short version）
  - terms.html:68（Refund promise）
  - index.html:106 的 p3，例如改成 "No alert? We extend free, then refund on request"
  - 中文版一起改。
- **本次改动之前付款的订单仍按旧条款**："If we never send you an in-stock alert during your watch, reply to any GetDuoNow email within 7 days after it ends for a full refund."
- 每退一笔亏约 $0.44 手续费。

**账目**：
- 单笔净收入：$4.99 − $0.44（Stripe 2.9%+30¢）− 短信约 $0.15 ≈ $4.40。
- 开了 Stripe Tax 的话，每笔再扣 0.5%（约 $0.025）。
- 拒付：每笔约亏 $20（$15 争议费 + 退回的 $4.99 + 手续费）。拒付率目标 <0.3%，退款率单独统计。
- 固定成本：Render $7.25 + Twilio 号码 $2.15 + 域名约 $1 ≈ $10/月。升 Resend Pro 后约 $30/月。
- 如果另开 LLC：注册费约 $45，每年还有约 $150 的阿肯色 franchise tax（未核实）。
- 关掉 Managed Payments 后，销售税要自己负责。部分州对 SaaS 或信息服务征税（未核实）。上线前在 Stripe Tax 里查一下要不要注册（未核实）。

## 4. 推荐与等级

**码与归因**：
- **推荐码**：
  - 沿用 referral.new_code()，改成一人一码：新建 sub 时，先找同一 email 已有的码复用。
  - 已经发出去的旧码继续有效。等级按 email 汇总这个人名下所有码。
- **分享链接 `/r/CODE`**：
  - 公开路由，加进门禁白名单。
  - 服务端种一个 30 天的 cookie `gdn_ref`，然后 302 到首页。`/?r=CODE` 也兼容。
  - 归因从 localStorage（index.html:129）改成这个 cookie。
  - 表单加一个"推荐码"输入框，手动输入的码优先于 cookie。
- 码写进 `metadata.ref`，付款后不再改归属。
- **内测门禁（⑩）**：共享 key 换成每人 3 个邀请码 `/i/CODE`。
  - 邀请码和推荐码分开：邀请码负责放行进站，同时带归因，算作邀请人的推荐。
  - 推荐码不限次数，公开上线后才是主入口。
  - 门禁期间，有人点 `/r/CODE`，只种归因，显示"内测中，需要邀请码"，不放行表单。
  - 门禁期每人最多 3 个推荐，所以 L3 暂时达不到，这是预期内的。需要多给邀请码，你在 admin 里加。
  - 这样第一天就知道谁带来了谁。这类归因事后补不回来。

**奖励**：
- **被推荐人**：付款即 +7 天（14 → 21）。这块要新增，现有代码只奖励推荐人。
- **推荐人**：当场 +7 天，或者存进 referral_credit（已有）。
- Phase 2 加一个选项：把存着的天数换成一张 7 天礼券送给朋友。已经抢到手的人更需要这个。

**有效推荐**（用来定等级）：
- 实付金额 >0。现有代码已经排除 comp 和免费开通。
- 手机号、邮箱、卡 fingerprint 都和推荐人不同。现有代码只比了邮箱和手机号。
- 已满 7 天，期间没有全额退款或拒付。
- 每人每月最多算 10 个。
- **不要求对方回过 Y**：短信不是购买条件，只收邮件的被推荐人也算。否则等于用推荐奖励变相逼人同意收短信。

等级不存字段，用 `level(n)` 现算，n = 这个 email 名下所有码的有效推荐数之和。等级变了就记一条 level_up，只发邮件通知。

**分享**：
- 用户点"抢到了"或回"抢到了"的那一刻，直接弹出分享页。管理页也放原生分享按钮和"复制文案"。
- 系统不替用户给朋友发短信（华盛顿州 CEMA 有诉讼风险）。
- **短信里不放码**：
  - referral._footer 本身只在 welcome 时才输出（referral.py:187）。
  - 问题出在 welcome 也走 Notifier.alert，所以短信里也带上了推荐链接（web.py:219）。
  - 改法是让这个 footer 只进邮件（①a，§5.2），**不是删掉它**。删掉的话 welcome 邮件里的推荐链接也没了。
  - 码只出现在 welcome 邮件、管理页和分享页。
- 小红书不让放外链，所以码要直接印在文案里：
  - 中：`我用 GetDuoNow 蹲 iPhone 门店自提库存。用我的码 K7M3QX 下单，我们各多 7 天：getduonow.com/r/K7M3QX（非 Apple 官方服务）`
  - 英：`I use GetDuoNow for iPhone pickup stock alerts. Use my code K7M3QX and we both get +7 days: getduonow.com/r/K7M3QX (Not affiliated with Apple)`

## 5. 短信与邮件

### 5.1 短信：Twilio toll-free + TFV
**费用与速度**：
- 号码 $2.15/月。
- 每段约 $0.0083，加运营商费，合计约 $0.012–0.013。收短信也按段计费。
- 每单约 10–12 段，约 $0.13–0.16，按 $0.15 算。这 10–12 段是：
  - CONFIRM、用户回的 Y、CONFIRMED
  - 约 4 条提醒
  - Phase 2 后每条提醒跟一条 FOLLOWUP，加上用户回的 1/2/3
- 这个估算的前提是每条短信都不超过 160 字符、纯 ASCII（§5.2）。现在的提醒正文约 230 字符，再加两个长链接，英文就要 3 段以上，中文要 6 段以上。
- 速度 3 条/秒，100 人约 33 秒。单次补货要发超过 150 人时，申请 High-Throughput Toll-Free。

**开通步骤**：
1. Twilio 升级成正式账号，充 $20，买一个 toll-free 号码。
2. 上线三个页面：
   - /terms、/privacy、/sms-terms 都必须加进门禁白名单。现在 /terms 不在 web.py:43-45 的白名单里，审核员打开是 401。
   - terms.html 里已经有 SMS terms 和 Privacy 两节（terms.html:73、89），/privacy 和 /sms-terms 做成别名或锚点就行。
   - 隐私政策写明"手机号不分享给第三方做营销"。
   - 截下结账页同意区的图。表单还在门禁后面，审核员看不到真实页面。
3. 提交 TFV：
   - 实体：LLC 名称，EIN 作为 BRN，类型 Private Profit
   - 用途：Account Notifications
   - 同意方式：Web Form，附截图链接
   - 预计月量：1k–10k，附样例短信

   被拒后 7 天内修改不用重新排队。审核要 3–5 个工作日到 1–3 周。
4. 建 Messaging Service：
   - inbound 填 `https://<域名>/twilio/inbound`（路由已有，门禁已经放行 /twilio/）。
   - 再配一个 status callback。
5. 审核通过后，在 Render 配 TWILIO_*，当天关掉 Mac 中转。

**结账页同意框**：
- 默认不勾，不勾也能买（只收邮件）。记下文案版本号 v1。
- 要替换掉现在"点开始即同意"的写法：文案在 index.html:112（英）和 121（中），显示在第 101 行。
- 文案：
> By checking, you agree to receive recurring automated stock-alert texts (incl. one follow-up per alert) from GetDuoNow at this number. Consent is not a condition of purchase. Up to 10 msgs/day during your watch. Msg & data rates may apply. Reply HELP for help, STOP to cancel. [Terms] [Privacy]

**回 Y 之前只发邮件**：付款成功页和管理页放一个预填 Y 的按钮 `sms:+1<号码>&body=Y`（iOS 写法，Android 兼容性未核实），减少不回 Y 造成的流失。

### 5.2 模板
**通用要求**：全部英文、纯 ASCII、不超过 160 字符，不放推广，不用公共短链。

**短信里不再拼任何 footer**：
- 给 hooks.footer_text 加一个 channel 参数，Notifier.alert 对短信和邮件分别拼 footer。
- 这三个 footer 都只进邮件：
  - _unsub_footer（optout.py:66-70）
  - got_footer（stories.py:51-55）
  - referral._footer
- 短信靠 "Reply STOP" 退订，不需要退订链接。

**welcome 和语言**：
- welcome 的短信就是下面的 CONFIRM，完整的 welcome 内容只发邮件。
- 现在中文用户收到的是中文短信：UCS-2 编码，每段只有 70 字符，成本是 2–3 倍。也改成英文。邮件仍按 lang 发中文。

**`<N>` 和 `<link>` 怎么填**：
- CONFIRM 里的 `<N>` 按实际天数填：Pass 14，Pass+ 30，Comp 按 admin 设的天数。
- ALERT 的链接：Phase 1 用 Apple 直达链接 `apple.com/shop/product/<part>`，Phase 2 换成 `getduonow.com/a/<token>`。
- 多个组合合并成一条时写 "+N more"，整条控制在 160 字符以内。

```
CONFIRM   GetDuoNow: Reply Y to confirm pickup stock alerts for your <N>-day watch. Msg freq varies. Msg&data rates may apply. Reply HELP for help, STOP to cancel.
CONFIRMED GetDuoNow: Confirmed. Alerts active until <date>. Reply STOP to opt out.
ALERT     GetDuoNow: iPhone Duo 256GB Black +1 more available for pickup at Wolfchase (Memphis), 2:41pm CT. <link> Reply STOP to opt out.
FOLLOWUP  GetDuoNow: Did you get it? Reply 1=Got it 2=Sold out 3=Didn't go. Reply STOP to opt out.
HELP      GetDuoNow stock alerts. Help: help@getduonow.com. Up to 10 msgs/day. Msg&data rates may apply. Reply STOP to cancel. Not affiliated with Apple.
RESUMED   GetDuoNow: Texts resumed until <date>. Reply STOP to opt out.
STOP      Twilio 路径：运营商自动回复，内容固定，我们不再发任何短信。
          Mac 中转路径：现在的文案是 "GetDuoNow: you're unsubscribed, no more alerts. To watch again, start a new watch on our site."（optout.py:31）。
          改成按渠道退订后，文案改为 "GetDuoNow: Texts stopped. Email alerts continue until your watch ends. Reply START to resume texts."
```

### 5.3 收短信（改 optout.handle_command）

| 回复 | 处理 |
|---|---|
| Y / YES | 从 START 集合里拿出来（optout.py:29）。如果这个号码当前是 stopped，就忽略、不改状态：运营商层面仍然拦着，我们这边改了会两边不一致。否则记 sms_confirm，回 CONFIRMED |
| STOP、STOPALL、UNSUBSCRIBE、CANCEL、END、QUIT、OPT OUT、REVOKE、退订、取消，以及整句命中正则 `stop\|unsubscribe\|don'?t text\|remove me` 的 | **只停短信**：写 suppression（reason=stop），记 sms_optout。**不再调 opt_out_phone**（现在会把这个号码下所有订阅 expire）。订阅保持 active，邮件照发。FCC 要求认可任何合理的撤回方式，最晚 10 个工作日处理，我们当场生效 |
| START / UNSTOP | 删掉 suppression 里 reason=stop 的那条，恢复短信，回 RESUMED。订阅一直是 active，不需要"恢复订阅"。本次改动前因为 STOP 被 expire 的付费订阅（如果有），用 admin 手工恢复 |
| HELP | 上线前实测 Twilio 是否已经默认回复（未核实），没有才回 HELP 模板，避免回两次。START 同理 |
| 1 / 2 / 3（Phase 2） | 写进这个用户最近一条提醒的 outcome；回 1 就发分享页邮件 |
| 其他 | 不回复，转发到你的邮箱（现在是直接忽略） |

**发送状态回调**：
- 21610：写 suppression（reason=stop），记 sms_optout。出现 21610 说明我们漏收了一条 STOP。
- 30007 / 30449：这条记 failed，改发邮件，并通知你。
- 每次发短信前，先查 sms_consent 和 suppression。

### 5.4 测试（问题 7）
1. **现在就能做（⓪ 先写成复现测试，①b 做完后应该全部通过）**：用 pytest 带签名 POST 以下内容：`Y`、`STOP`、`pls stop texting`、`START`、`HELP`、`1`、乱码。检查：
   - STOP 之后订阅仍是 active，邮件照发，suppression 里有记录。
   - STOP 之后回 Y，仍是 stopped。
   - START 之后短信恢复。
   - POST /unsub 只设 email_off，短信照发。
   - 模拟一个 ErrorCode=21610 的 status callback，号码进 suppression。
   - 同时改掉 test_optout 里按旧语义写的断言。
2. **TFV 通过后，用自己的手机走一遍**：
   1. admin 开一个 comp。
   2. 收到 CONFIRM，回 Y，查 sms_confirm。
   3. 调 `POST /admin/test-alert/<sub>`。
   4. 回 1，查 outcome（Phase 2）。
   5. 回 STOP，查 suppression，确认订阅仍是 active。
   6. 再触发一条提醒：我们发送前查到 suppression，直接跳过短信，只收到邮件。
   7. 回 Y：仍然收不到短信。
   8. 回 START，再触发一条，短信恢复。

   21610 分支（我们漏收 STOP 的情况）只在 pytest 里模拟测，真机上走不到。
3. **Mac 中转期**：
   - sms_relay.py 读 chat.db 回传，所以 Y/STOP 现在就能自动处理（按 ①b 的新语义）。
   - 中转只给不超过 20 个勾过同意框的熟人用，TFV 通过当天关掉。
   - 对外开放必须等 Twilio。

### 5.5 邮件：Resend
**费用**：
- 免费档每月 3,000 封，但每天 100 封封顶，超了的提醒全丢。
- followup 改成只走邮件后，每次补货，每个被提醒的人要收"提醒 + 卖完"两封。4 次补货/天 × 20 人 × 2 = 160 封，已经超过上限。
- 所以付费用户超过 10 人，或某天超过 50 封，就升 Pro（$20/月 50k 封，超出 $0.90/1k）。Pro 也列在公开上线门槛里。

**开通**：
- 从子域名 alerts.<域名> 发信，区域选 us-east-1。
- DNS：加 DKIM TXT，以及 send. 子域名上的 MX 和 SPF。`_dmarc` 先设 p=none，全部 pass 后改成 quarantine。
- API key 只给 sending 权限，限定这个域名。Render 里配 RESEND_API_KEY、EMAIL_FROM、reply_to。
- 注册 Google Postmaster Tools。
- 给 Gmail、Yahoo、Outlook 各发一封，检查 SPF/DKIM/DMARC 是否 PASS、是否进收件箱。进了垃圾箱就换 Postmark，改约 5 行。

**每封邮件**：
- 一键退订：
  - List-Unsubscribe 和 List-Unsubscribe-Post 头已经有了（optout.py:62-63）。
  - 问题是它们指向的 POST /unsub 会把整个订阅 expire（optout.py:122-127）。改成只给这个订阅设 email_off（①b）。
  - unsub.html 的文案从 "no longer get texts or emails" 改成 "No more emails for this watch. Texts (if on) continue."
- 补上 `Idempotency-Key: alert-<id>`，关掉打开和点击追踪。
- **只走邮件**的三类：followup.py 里的到期提醒（带续期链接，属于推销）、每周 heartbeat、"N 分钟卖完"。现在它们经 Notifier.alert 也会发短信，要给 Notifier.alert 加 channels 参数，这几类只传 email。
- 页脚：`You started a watch on <date>. Stop email alerts: <link>. Not affiliated with Apple Inc. <LLC>, <实体地址>`。level_up 这类邮件带营销性质，CAN-SPAM 要求写实体地址。

## 6. 实时库存架构与扩容

**现状**：
- 单线程，顺序请求，每个请求新建一个 httpx.Client（apple.py:92），0.6–1.0 秒一个。
- 提醒在同一线程里同步发出（poller.py:55-59），超时各 20 秒，一次补货发很多人会把其它店的检查往后推。
- 每个 cell 单独 commit 一次。
- 50 店 × 40 part = 每轮 250 个请求，要 150–250 秒，赶不上"3 分钟卖完"。

**Phase 1（本周 ⑤）**：
- 共用 Client（约 0.32 秒一个），开 8 并发，250 个请求约 10 秒。
- 顺序：
  1. 并发抓取，结果放在内存里。
  2. 在一个短写事务里做 availability upsert 和插入 alerts，然后提交。
  3. **提交之后**再发通知。
- 发 HTTP 请求期间不持有写锁。否则 Stripe webhook 的 activate 会报 `database is locked`：写事务开着 10 秒，WAL 加 busy_timeout=5000 也挡不住。
- 记 round_ms。
- 轮询间隔 = max(5, 30 − 本轮耗时) + 0–3 秒随机抖动，慢轮之后也留出间隔。
- 监控：
  - poller 每轮结束 ping 一次 healthchecks.io。它免费，可以设成"多久没收到心跳就报警"，周期按它允许的最短值设（能不能短到 90 秒未核实）。
  - /healthz 返回 last_cycle_age，超过 90 秒返回 503，给手动检查和以后的付费 UptimeRobot 用。免费版 UptimeRobot 每 5 分钟才查一次，也不会读 JSON 里的数字，做不到 90 秒报警。
  - Render 的 healthCheckPath **不要**指向 /healthz（不设，或者指向一个永远返回 200 的路径）。否则慢一轮就可能被重启，或被判部署失败。
  - 某家店连续 3 轮出错，也通知你。

**Phase 2**：
- 发送放进独立的队列线程：短信 3 条/秒，邮件并发。
- SLO：从检测到首条短信发出 p95 <10 秒；100 人 45 秒内发完；每轮 <15 秒。
- 达标后，对外文案才能写"几秒内"。

**对 Apple 克制**：
- 只查有人盯的组合，加上硬上限。
- 遇到 403/429 就退避，冷门组合降到 60 秒一次。
- 不用代理绕。
- 单个 Render IP 每天几十万请求会不会被封，目前不知道。

**扩容顺序**：
1. 每轮 p95 超过 20 秒：先验证 `location=<ZIP>` 是否每家店都返回全部 part。齐的话，同城的店合并成一个请求。
2. 申请 Twilio 高吞吐。
3. 需要第二个实例、独立 worker，或部署停机已经让人漏了补货：迁到 Render Postgres（$6/月）。

**部署**：
- Render 关掉 autoDeploy，否则白天一 push 就会停机。改成凌晨 2–6 点 CT 手动 deploy。
- 有持久盘就做不到零停机。Stripe webhook 会自动重试，不会丢。

## 7. 数据模型、事件日志、指标

SQLite 放在 /var/data，`db.connect()` 里加 `PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;`。

暂不建 users 表：现有 feature 都按 email 归并，前 100 个用户够用。一人多监控变乱时再加。

```sql
-- subscriptions +plan(pass|passplus|comp|gift), stripe_customer_id, card_fingerprint, extended_free, email_off
--   extend 克隆新 sub 时，email_off 等用户设置一起复制
-- watches +family
-- alerts +token, channel, status(queued|accepted|delivered|failed), attempts, outcome(got|missed|skipped)

-- events 现有表结构是 (id, at, event, sub_id, data)（admin.py:31-37）。只加列，不改名，admin.py 的查询不用重写。
-- setup() 里先查 PRAGMA table_info 再 ALTER，重复启动不报错。
-- SQLite 的 ADD COLUMN 不能带 UNIQUE，idem 用部分唯一索引实现。
ALTER TABLE events ADD COLUMN email_hash TEXT;   -- 加盐 HMAC，按人汇总用
ALTER TABLE events ADD COLUMN ref TEXT;          -- alerts.id / Stripe id / Twilio SID
ALTER TABLE events ADD COLUMN channel TEXT;      -- sms|email|web|stripe|twilio|admin
ALTER TABLE events ADD COLUMN idem TEXT;         -- 外部事件 id，INSERT OR IGNORE 去重
CREATE UNIQUE INDEX IF NOT EXISTS ev_idem ON events(idem) WHERE idem IS NOT NULL;
CREATE TRIGGER IF NOT EXISTS ev_no_upd BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'append-only'); END;
CREATE TRIGGER IF NOT EXISTS ev_no_del BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'append-only'); END;
-- 现有代码没有 UPDATE 或 DELETE events 的地方（已 grep），加触发器不会弄坏现有功能。

consents(id, sub_id, phone, text_version, ip, ua, checkout_session, created_at);  -- 留 4 年
suppression(phone_hash PK, ts, reason);  -- reason: stop|deleted。唯一的短信黑名单，删号后也保留
-- sms_consent 只存 on / confirmed，不再存 stopped。stopped 只记在 suppression 一处，避免两份不一致
-- 已有: sms_consent, referral_codes, referrals(+void_reason), referral_credit, extensions, followup_log, success_stories, sms_queue
```

**事件**（写在 `event` 列）：
- 已有：subscription.created/activated、alert.sent、subscriptions.expired、cell.soldout。cell.soldout 带 since 和 minutes，本身就是补货历史，不再另建 restocks 表。
- 要补：sms_optin、sms_confirm、sms_optout、payment、refund、dispute、upgrade、cancel、email_off、alert_delivered、alert_failed、clicked、got_it、referral_paid、level_up、deleted。
- IP 和 UA 只存进 consents，events 里不放 PII。

**送达分三级**：
- 渠道没配置：记 failed，不再兜底返回成功（notify.py:63-72、91-92）。
- 服务商返回 2xx：记 accepted。
- Twilio 回调 delivered：才算 delivered。
- Mac 中转的 sms_queue 只记 queued。
- **重试上限**：
  - 现在 subscribers_to_alert 只把 `a.ok=1` 当成已发（db.py:149）。failed 变成真 failed 之后，有货期间每 30 秒就会重发一次。比如只收邮件的用户碰上 Resend 429，会一直重复请求。
  - 改成：同一段有货期内（alerts.sent_at ≥ availability.since），这个订阅已经有 queued/accepted/delivered 的记录，或者已经尝试满 3 次，就不再发。

**点击**：
- `/a/<token>` 先记 clicked，再 302 到库里存的 Apple 链接。不读 URL 参数，防开放重定向。
- 按 UA 过滤链接预览和爬虫，只算第一次真人点击。
- "抢到了"和退订页面都是 GET 只打开页面、POST 才记录。

**每人指标**：按 email 汇总（subscriptions.email，或 events.email_hash）。包括：
- 提醒数、送达数、点击数、抢到数
- 点击延迟（clicked − alert.sent）
- 有效推荐数 → level()
- 当前档、到期日、短信状态、邮件状态

**全局指标**：
- 从检测到首条发出的 p50/p95
- 短信送达率、30007 比例
- 补货持续时间中位数
- 回复者里抢到的比例
- 复购率、带码订单占比
- 退款率、拒付率

**备份**：
- Render 每天自动快照，保留 7 天。
- 每晚用 `VACUUM INTO` 导出，经 `/admin/backup` 由 Mac 拉回，保留 30 天。
- 用独立的 BACKUP_TOKEN（不复用 ADMIN_TOKEN），每季度轮换。每次拉取记一条事件，并发邮件通知你。

**删除请求**：
1. 手机号哈希写进 suppression（reason=deleted）。之后同一个号码重新勾选同意，可以清掉 deleted 这条，但清不掉 stop 那条（只有 START 能清）。
2. 清空 subscriptions 里的 phone 和 email，删掉 watches。下面这些表里这个人的数据也删除或清空：
   - sms_consent（明文手机号）
   - sms_queue（手机号加短信内容）
   - referral_credit（email 是主键）
   - referral_codes
   - success_stories（名字、城市、留言）
3. events 本来不含 PII（email_hash 是加盐 HMAC），留下来就是匿名数据。
4. consents 按 TCPA 诉讼时效保留 4 年，隐私政策里写明。
5. Stripe 的付款记录要报税，保留。异地备份 30 天内自然滚掉。

## 8. 成功案例（合规）
- **数据先行（本周 ⑪）**：
  - /stories 和 /api/stories/summary 已经上线，且公开在门禁外。
  - 要改：_public（stories.py:96）也展示同意展示的"没抢到"。
  - 顶部数字改成从 cell.soldout 和 alerts 实时算，比如"本周抓到 N 次补货、Memphis 中位 X 分钟卖完"，不挑样本。
  - 用户自报的抢到数单独标注"用户自报"。
- **回访**：
  - Phase 1：提醒**邮件**里放 /got 链接。got_footer 通过 channel 参数只进邮件，不进短信，免得超过 160 字符。
  - Phase 2：补货结束后、最晚 3 小时，发 FOLLOWUP 短信。同意文案里已经写了，算在每天 10 条里。
- **展示同意**：
  - 用户说抢到后，在单独的页面问能否展示"名字首字母 + 城市 + 机型"，用户可以自己写一句话。
  - 存下同意时间和当时展示的原文，用户可以撤回。
  - 不替用户写评价，奖励不以说好话为条件。
- **分享页 / 图卡**：
  - Phase 2 先做分享页，Phase 3 再用 Pillow 出 1080×1350 的 PNG。
  - 只用这个用户的真实数据，例如 "Memphis · got it 2 min after alert"。
  - 用通用手机剪影，不用 Apple logo 和官方产品图。
  - 印推荐码时，同时印上披露 "Code gives us both +7 days"（FTC 利益关系披露）和 "Not affiliated with Apple"。
- **故事墙**：
  - 只读，也收"没抢到"的。
  - 拿过奖励的，标"分享获赠 7 天"。
  - 上线前不放占位评价，v1 不开评论区。

## 9. 风险与对策

| 风险 | 对策 |
|---|---|
| Apple 商标 / 律师函（2013 年有先例） | 品牌、域名、账单名、图卡都不用 Apple/iPhone 标识。"Duo"跟 Apple 产品名重合，品牌要在提交 TFV 前定死：TFV、Stripe、域名都绑品牌，事后换就要重审。收到律师函：24 小时内停轮询，按剩余天数退款，再换名。Apple 网站条款是否禁止自动抓取未核实，所以查询保持克制 |
| TCPA（每条法定赔偿 $500–1,500，未核实） | consents 证据留 4 年；回 Y 二次确认；STOP 之后回 Y 不能恢复；认可自然语言撤回；suppression 黑名单；短信里不夹推广，包括推荐码 |
| 连累主业 | END ARCHIVE LLC 是转售主业。建议给 GetDuoNow 另开一家阿肯色 LLC（注册费约 $45，每年约 $150 franchise tax，均未核实），Stripe 和 TFV 直接用新实体 |
| 拒付（每笔约亏 $20） | 独立 Stripe 账户，账单名 GETDUONOW；0 提醒自动延期；24 小时内可退；拒付就停服务并收回推荐奖励 |
| 漏提醒 | healthchecks.io 心跳；/healthz 超时返回 503；notify 不再假报成功；失败最多重试 3 次；Resend 撞日上限之前就升级 |
| 付费用户误发 STOP 后连邮件也没了 | ①b 改成按渠道退订：STOP 只停短信 |
| 单个 IP 被 Apple 封 | 退避、冷门组合降频、按 ZIP 合并查询 |
| 推荐被刷 | 手机号、邮箱、卡 fingerprint 三项去重；全额退款或拒付就收回奖励；每月上限 10 个；奖励只给天数 |
| 备份泄露 | 独立 token、定期轮换、每次拉取都通知你 |

## 10. 分阶段计划

### 本周（约 4 人日；⓪–④ 做完就能开始收钱）

| # | 内容 | 文件 | 工时 |
|---|---|---|---|
| ⓪ | 先写复现测试（features 已在 4fc0f09 提交，`.venv/bin/python -m pytest -q` 118 passed）：<br>• STOP 不 expire 订阅；STOP 之后回 Y 不恢复<br>• /unsub 只关邮件<br>• 短信不带 footer、不超过 160 字符<br>• failed 提醒最多试 3 次<br>• 部分退款不停服务<br>• 升级后到期日 = 原到期日 + 16 天 | tests/* | 2h |
| ①a | 合规修正：<br>• footer 分渠道：hooks.footer_text 加 channel 参数；_unsub_footer、got_footer、referral._footer 只进邮件<br>• welcome 的短信改成 CONFIRM<br>• followup 只走邮件（Notifier.alert 加 channels 参数）<br>• Y 单独记事件；STOP 认自然语言<br>• 短信统一用 §5.2 的英文模板（天数用变量）<br>• 被推荐人 +7 天 | features/{referral,stories,followup,optout}.py, hooks.py, notify.py, messages.py | 3h |
| ①b | 渠道级退订：<br>• STOP 只写 suppression，不调 opt_out_phone<br>• Y/YES 移出 START 集合，号码是 stopped 时忽略<br>• /unsub 和 List-Unsubscribe 只设 email_off（加列）<br>• 改 Mac 中转的 STOP 回复文案和 unsub.html<br>• 改 test_optout | features/optout.py, db.py, templates/unsub.html, tests/test_optout.py | 2h |
| ② | • 修四处假成功（notify.py:63-72、91-92），送达状态分级<br>• alerts 加 status 和 attempts，同一段有货期最多试 3 次（改 db.py:149）<br>• 加 Idempotency-Key | notify.py, db.py | 2h |
| ③ | • 同意框（默认不勾）、consents 表<br>• /terms、/privacy、/sms-terms 公开：加进白名单，后两个做成别名<br>• 退款文案改新政策，写明旧单按旧条款（terms.html:45、68，index.html:106）<br>• 截图 | index.html, terms.html, web.py, db.py | 3.5h |
| ④ | • 新 Stripe 账户的 key<br>• 按邮箱复用 Customer<br>• 从 latest_charge 取卡 fingerprint 存进 subscriptions<br>• charge.refunded 只在全额退款时停服务、收回奖励<br>• dispute、expired<br>• event.id 去重<br>（paid 校验已有） | web.py, db.py, features/referral.py | 3h |
| ⑤ | • 共用 Client、8 并发<br>• 抓取 → 短写事务 → 提交后再发通知<br>• 轮询加抖动<br>• /healthz 返回 last_cycle_age，超过 90 秒返回 503<br>• poller 每轮 ping healthchecks.io | apple.py, poller.py, db.py, web.py | 4h |
| ⑥ | • WAL + busy_timeout<br>• events 加列、部分唯一索引、触发器<br>• 加 plan 和 family 字段（系列选择器前端已有） | db.py, features/admin.py | 2h |
| ⑦ | 管理页 /m/<token>（按 email 签名），内容：<br>• 这个 email 名下所有监控和状态<br>• Stop alerts<br>• 推荐码和 credit<br>• 回 Y 按钮、续期链接<br>• 自己的事件时间线 | 新 features/manage.py, templates/manage.html, signing.py | 4h |
| ⑧ | • 0 提醒自动延期<br>• 每天 10 条短信上限<br>• 同店合并 | poller.py, db.py | 2h |
| ⑨ | /admin/backup + BACKUP_TOKEN + Mac launchd 定时拉取 | features/admin.py, scripts/ | 1h |
| ⑩ | • 邀请码替换共享 key（每人 3 个 /i/CODE，自带归因）<br>• /r/CODE 公开路由 + 30 天 cookie；门禁期只种归因，不放行表单<br>• 推荐码一人一码（同一 email 复用）<br>• 表单加推荐码输入框 | web.py 中间件, features/referral.py, index.html | 2h |
| ⑪ | /stories：<br>• _public 也展示同意展示的"没抢到"<br>• 顶部数字改成从 cell.soldout 和 alerts 算<br>• 自报的抢到数单独标注 | features/stories.py | 1.5h |

### 下阶段（TFV 通过后，第 2–3 周，约 4 人日）
- **Twilio 正式发送**（改 notify.py、optout.py、poller.py，1.5 天）：
  - 发送走 Messaging Service。
  - status callback：21610 → 写 suppression；30007/30449 → 改发邮件。
  - 发送队列线程（3 条/秒，随机顺序）。
  - 回过 Y 才发短信。
  - 关掉 Mac 中转。
- **套餐与升级**（改 web.py、db.py，1 天）：
  - config.py 加 `PLANS`（pass/passplus/comp/gift）。
  - 限额校验。
  - 升级 Checkout（+16 天）。
  - 24 小时内自助退款。
- **推荐规则**（改 referral.py，1 天）：
  - 有效推荐：三项去重（含卡 fingerprint）、7 天内无全额退款或拒付、每月上限 10 个。
  - 等级按 email 汇总所有码来算。
  - level_up 邮件、礼券兑换。
- **点击与回访**（改 stories.py、admin.py，0.5 天）：
  - `/a/<token>` 点击跳转。
  - FOLLOWUP 短信和 1/2/3 回复处理。
  - 展示同意页、分享页。
  - `/admin/test-alert`。

**公开上线门槛**（全部满足才撤门禁）：
- TFV 已通过
- 独立 Stripe 账户已开
- 三个政策页已上线，且在门禁外能打开
- 邮件送达测试已通过
- SLO 连续 3 天达标
- Resend 已升 Pro
- Render 的 autoDeploy 已关

### 之后（看到信号再做）

| 做什么 | 触发条件 / 说明 |
|---|---|
| 分享图卡（Pillow 1080×1350） | 分享页跑起来后，约 1 天 |
| 按 ZIP 合并查询 | 每轮 p95 超过 20 秒 |
| 迁 Postgres + 独立 worker | 需要第二个实例时 |
| Hunter $7.99/月 + Stripe Portal | 复购率 ≥20% |
| UptimeRobot 付费 1 分钟档（约 $7/月） | healthchecks.io 不够用时 |
| Twilio 高吞吐 | 单次补货超过 150 人 |
| 换 Telnyx | 每月短信超过 5 万条 |
| 实测微信分享卡效果 | — |

**明确不做**：密码登录、付费换更快提醒、常驻免费档、公开评论区、第三方推荐服务、Stripe 免费试用、10DLC、短号码、短信里放推广。

## 11. 需要老板本人做的事

**第 1 天，按顺序做（后面的事都绑在前面的决定上）**：
1. **定实体**：要不要另开 LLC（推荐开）。注册费约 $45，每年约 $150 franchise tax（未核实）。EIN 网上当天能拿到（未核实）。
2. **定品牌**：保留 GetDuoNow，还是换成不含 Duo/Apple/iPhone 的中性名，把 getduonow.com 留作跳转。
3. **买域名**，DNS 放在 Cloudflare。准备好 help@ 收件邮箱和一个实体地址（邮件页脚要用）。
4. **开独立 Stripe 账户**，账单名 GETDUONOW。把 key 和 webhook secret 配到 Render。顺便确认销售税要不要注册（Stripe Tax 每笔另收 0.5%）。
5. **Twilio**：升级正式账号，充 $20，买一个 toll-free 号码。政策页上线后提交 TFV。
6. **Resend**：注册，添加域名。

**之后**：
- 开 healthchecks.io 账号（免费），把 ping URL 配到 Render。
- Render：关掉 autoDeploy；确认 healthCheckPath 没有指向 /healthz。
- 确认 Mac 中转名单：不超过 20 人，且都勾过同意框；Mac 要给 sms_relay 开"完全磁盘访问权限"。
- 拍板 Pass+ 的价格和 L1–L3 的门槛。
## 未来方向（用户 2026-11-02）
- 同一套「到货秒通知」扩展到 **Pokémon 卡、Costco、Target、书店** 等。用户原话：「这个服务不错」，先记下，暂不做。
- 复用：付款、提醒（短信/邮件）、多语言、推荐、日志；每个品类只需换库存数据源 + 商品目录。
