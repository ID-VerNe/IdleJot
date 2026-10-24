# 水群专属目录 (Water Group Hub)

本目录为 闲笔 (IdleJot) QQ 机器人水群专属资产与配置中心，专为 Antigravity 挂机水群设计。

---

## 目录结构

```
water-group/
├── personas/           # 人格卡中心（即时热重载）
│   ├── little_whale.md     # DeepSeek 小鲸鱼（默认）
│   ├── dragon_girl.md      # GPT 白龙娘
│   ├── little_whale_v2.md  # 小鲸鱼2号
│   └── template.md         # 新建角色卡模版
├── stickers/           # 表情包缓存与收藏图库
├── sync_personas.py    # 人格卡同步工具（Agent 会自动调用）
└── README.md           # 本说明文件
```

---

## 如何添加或修改人格卡？

1. 直接在 `water-group/personas/` 目录下复制 `template.md`，新建如 `my_role.md`。
2. 按照模板编写人设、说话铁律和示范对话。
3. 在 Antigravity 对话框直接对我说：**“换成 my_role 人格水群”**，即可秒级热重载并生效！

---

## 快捷指令速记

在 Antigravity 中可直接使用以下口令唤醒水群操作：
- **`开始水群`** / **`挂机水群`**：自动连接当前测试群，使用默认人设挂机水群。
- **`换成白龙娘水群`**：无缝切换为人设 `dragon_girl`。
- **`停止水群`**：退出当前聊天会话。
- **`/goal 挂机水群`**：长效自主任务模式，适合长时间不间断挂机。
