# mipmap

> 渐进式 TLDR——让长 AI 回答和长文章变得可以"快速浏览"。

`mipmap` 把任意一段长文本变成一组由短到长的摘要：从一句话的浓缩开始，每一层是前一层的两倍，最长一层接近原文的 15%。读者从最短的开始扫，觉得够了就停，想了解更多就继续往下读。整套过程由本地 LLM（默认是 ollama 上的 qwen2.5-coder:14b）流式生成，最短的 TLDR 大约 1 秒就能看到。

灵感来自计算机图形学里的 mipmap 纹理压缩——每一级的分辨率是上一级的一半。文本的"分辨率"则用词数（英文）或字符数（中文）衡量。

## 为什么做这个

跟 AI 对话经常拿到几百到上千词的长回答，一眼读不完；想知道大意又不想全读，直接说"TL;DR"又只能拿到一句话——想再多看一点没办法。读博客、Wikipedia、技术文章也是同样的问题：固定长度的摘要总是要么太短要么太长。

mipmap 的思路是把"摘要的详细程度"做成一条连续的滑动条：从一句话的标题级开始，每往下一层信息量翻倍，你想读多深就读多深。读到觉得够了，Ctrl-C 走人。

## 安装

依赖：

- [`uv`](https://github.com/astral-sh/uv)（脚本通过 uv-script shebang 自管 Python 环境）
- [`ollama`](https://ollama.com)，并拉好一个支持中英双语的模型：

```bash
ollama pull qwen2.5-coder:14b
```

把脚本拉下来，建个软链就好——`mipmap.py` 是一个单文件 uv-script，第三方依赖为零：

```bash
git clone https://github.com/archibate/mipmap.git
cd mipmap
chmod +x mipmap.py
ln -s "$PWD/mipmap.py" ~/.local/bin/mipmap
```

确认 `~/.local/bin` 在 `$PATH` 里就能直接用 `mipmap`。

## 使用

最简单的用法——把 stdin 喂给它：

```bash
cat article.md | mipmap
```

或者传文件路径：

```bash
mipmap article.md
```

输出格式有四种，默认是 `plain`（原始格式，带分隔符）：

```bash
mipmap article.md -f plain        # 原始：带 --- LEVEL N --- 分隔符，方便管道处理
mipmap article.md -f color        # 16 色：第一层最亮，往下逐渐变暗
mipmap article.md -f color-256    # 256 色渐变：从亮到暗的平滑过渡
mipmap article.md -f jsonl        # 每层一个 JSON 对象，方便机器解析
```

`color` 模式会把分隔符直接隐去，完全靠颜色区分层级——L1 最亮，L7 几乎融进背景色。视觉上就像图形学里的 mipmap 一样自然衰减。

### 实际效果

跑 Paul Graham 的《Founder Mode》（约 1250 词）：

```
$ mipmap founder-mode.txt -f color-256 -v
mipmap: source 1247 words, computing 5 levels: 20, 40, 80, 160, 187 (qwen2.5-coder:14b)

[最亮]  Founder mode, distinct from manager mode, is crucial for scaling
        startups effectively.

[较亮]  Founder mode involves direct CEO involvement and differs significantly
        from conventional management practices...

[正常]  Founders like Brian Chesky discovered that running a company in founder
        mode—inspired by leaders like Steve Jobs—yielded better results than
        following conventional wisdom...

[较暗]  Founder mode breaks the principle of CEOs engaging only through direct
        reports, advocating for skip-level meetings and annual retreats...

[最暗]  The concept of founder mode is still emerging, with limited literature
        or formal understanding. Founders have achieved significant success
        despite bad advice...
```

只读到第一层就够了的话，Ctrl-C 走人即可。

### 中文支持

支持中文（以及日韩 CJK 文字）。会自动检测源文本的语言——如果 CJK 字符占比超过 50%，自动切换到：

- 改用字符计算长度（最短一层 30 字）
- 中文版的 prompt（这一点很关键，英文 prompt 生成中文容易出翻译腔）

也可以用 `--lang zh` / `--lang en` 强制指定。

```
$ cat 中文文章.md | mipmap -v
mipmap: source 1234 字, computing 4 levels: 30, 60, 120, 185 (qwen2.5-coder:14b)
...
```

## 常用参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `-m`, `--model` | `qwen2.5-coder:14b` | ollama 模型名 |
| `-e`, `--endpoint` | `http://localhost:11434` | ollama 服务地址 |
| `-f`, `--format` | `plain` | 输出格式：`plain` / `color` / `color-256` / `jsonl` |
| `-c`, `--compression` | `0.15` | 最长一层的压缩比，默认源文本的 15% |
| `--max-levels` | `7` | 层级上限。再长的文本也不会超过 7 层 |
| `--floor` | 20 / 30 | 最短一层的长度，英文按词、中文按字 |
| `-t`, `--temperature` | `0.4` | 采样温度 |
| `--seed` | 随机 | 固定种子用于复现 |
| `--lang` | `auto` | 强制语言：`auto` / `en` / `zh` |
| `-v`, `--verbose` | | 打印 stderr 上的层级规划信息（默认安静） |

环境变量也能设默认值：`MIPMAP_MODEL` / `MIPMAP_ENDPOINT` / `MIPMAP_FORMAT` / `MIPMAP_COMPRESSION` 等。`NO_COLOR=1` 会自动把 color 模式降级为 plain（遵循 [no-color.org](https://no-color.org) 约定）。

## 工作原理

一次模型调用，prompt 让模型按从短到长的顺序输出所有层级，用 `--- LEVEL N ---` 分隔。CLI 流式接收并实时渲染，L1（最短）大约 1 秒就可见。

层级数量按源文本长度自适应：

- 短文本（< 20 词）：原样输出，不调用模型
- 中等（20-266 词）：只生成一层 TLDR
- 长文本：多层 mipmap，每层是前一层的两倍，直到接近 15% 上限

prompt 里有几个关键设计：

- **强制陈述或祈使语气**——避免"本文讨论了..."这种万年不变的元描述开头，直接陈述结论。
- **从短到长生成**——TLDR 先于详情产出，延迟低，读到够了 Ctrl-C 立刻打断，省下后面几层的生成时间。
- **格式说明放在源文本之后**——长输入下模型容易"忘"前面的指令，放后面更稳。

## 已知限制

- **数据表格主导的文本不太合适**。模型在高密度数据列表上（比如一份 200 行的工具列表）会压成一段笼统描述，不会枚举具体条目。这种输入拿到的是一个好用的 TLDR，加上几层几乎重复的扩写。
- **大输入会被截断**。超过约 24000 字符的源文本会从末尾截掉，stderr 会有提示。本地小模型 8K context 的折中。
- **字数目标不严格满足**。模型对"约 N 字"的执行力不完美——通常会偏小 30-50%（在信息密度有限的源文本上更明显）。当作目标提示，不是硬约束。

## 致谢

灵感和命名都直接来自计算机图形学里的 mipmap 纹理。

## 许可证

MIT
