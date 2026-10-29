# PaperHarbor · 文献港

面向英文数学与计算数学研究的 Windows 桌面文献工具，集中完成论文检索、公开全文与补充材料下载，以及引用导出。

[下载 Windows 版 v0.2.0](https://github.com/lasheantpy/-/releases/tag/v0.2.0) · [使用说明](使用说明.html) · [反馈问题](https://github.com/lasheantpy/-/issues)

## 下载与启动

普通使用者下载 Windows 便携版即可，无需安装 Python。

1. 打开 [版本发布页面](https://github.com/lasheantpy/-/releases/tag/v0.2.0)，在 **Assets** 中下载 **`PaperHarbor-0.2.0-Windows-x64.zip`**。
2. 将 ZIP **完整解压**到可写入的文件夹。
3. 进入解压后的 `PaperHarbor-0.2.0` 文件夹，双击 **`启动文献港.cmd`**。

也可以运行 `dist/PaperHarbor/PaperHarbor.exe`。请保留整个解压目录，程序需要同目录中的运行库与配置文件。

发布页中的 `Source code (zip)` 和 `Source code (tar.gz)` 是源码快照；日常使用请选择名称带 **Windows-x64** 的软件包。配套的 `.sha256` 文件用于校验下载包。

## 基本使用

1. 输入英文关键词、论文完整标题或 DOI，勾选来源，点击 **「搜索论文」**。
2. 按需设置年份和排序，或在当前结果中筛选期刊、出版社；需要扩大结果范围时点击 **「更多结果」**。
3. 勾选结果第一列的方框，选择要下载或导出的论文。点击标题查看详情不会自动勾选论文。
4. 确认保存目录，点击 **「下载所选正文 + 补充材料」**。
5. 完成后使用 **「打开正文 PDF」** 或 **「查看下载结果」**；也可以导出勾选条目的 CSV、BibTeX 或 RIS。

默认下载目录为程序根目录下的 `library/`，可以在界面中修改。设置和检索缓存保存在 `data/`。

## 主要功能

- **多来源检索**：汇总六个学术索引与仓储来源，支持关键词、标题和 DOI 查询。
- **分页与状态提示**：合并去重后最多显示 500 条，显示各来源的请求结果、缓存、限流或连接失败状态。
- **论文去重**：结合 DOI、arXiv 标识等信息识别重复记录，避免仅凭同名同年合并不同论文。
- **期刊与出版社信息**：显示可取得的期刊、出版社和 ISSN，并支持当前结果中的文字筛选。
- **出版社网站补查**：提供九个官方出版平台入口，方便在浏览器中继续检索。
- **公开资料下载**：尝试取得公开正文 PDF 与可识别的补充材料，校验文件后保存。
- **引用与下载报告**：导出 CSV、BibTeX、RIS 和来源记录，并生成可阅读的 HTML 下载报告。

## 检索来源

| 自动汇入来源 | 主要用途 |
| --- | --- |
| OpenAlex | 跨学科学术索引、引用信息及公开版本线索 |
| arXiv | 数学及相关学科预印本 |
| Crossref | DOI 登记的期刊、图书、会议等书目信息 |
| OpenAIRE | 研究成果与机构仓储聚合 |
| HAL | 开放学术仓储，包含数学及相关研究 |
| Europe PMC | 生命科学与医学文献，适合生物数学等交叉主题 |

默认启用 OpenAlex、arXiv、Crossref 和 OpenAIRE；可按研究方向调整来源。

出版社网站入口包括 **Elsevier / ScienceDirect、Springer Nature Link、Wiley Online Library、SIAM、AMS、Cambridge Core、Oxford Academic、Taylor & Francis、De Gruyter Brill**。

这些入口在浏览器中打开官方页面，网站结果不会自动汇入软件。多数入口会携带当前关键词；AMS 使用官方期刊门户，需要复制关键词后在网站内检索。

## 检索与下载的说明

- 各来源的覆盖范围、更新时间和查询语法不同；一次检索及 500 条上限均不代表穷尽文献。可通过同义词、标题和年份分组补查。
- 期刊与出版社筛选只作用于当前结果，不会向远端发起新查询。
- 检索缓存有效期为 24 小时；来源请求失败时可能恢复旧缓存，界面会标明。外部服务限流、超时或访问限制可能影响结果。
- 找到书目不等于能够下载全文。程序仅尝试公开可访问的链接；部分期刊论文可能只取得预印本或作者接收稿，请查看详情中的版本与出处。
- 正文会检查 PDF 格式并解析页面；保存的 JSON 是来源与状态记录，不能代替论文正文。
- 补充材料支持 PDF、ZIP、CSV、TXT、XLSX、DOCX 和常见图片。ZIP 附件只做校验，不自动解压或执行。
- 再次下载时会检查并跳过有效的已有文件；取消后保留已完成文件，未完成项目可重试，不支持字节续传。
- HTML 报告反映生成时的文件状态；之后移动或删除资料，旧报告不会自动更新。

详细操作可在下载包中打开 **`使用说明.html`** 查看。

## 从源码运行

源码运行与打包面向 **Windows**，开发及验证使用 **64 位 Python 3.12**。需要带有 Tkinter 的 Python 安装。

下载或克隆仓库，在包含 `launch.pyw` 和 `requirements.txt` 的源码根目录打开 PowerShell，执行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe launch.pyw
```

直接调用虚拟环境中的 Python 即可，不需要执行激活脚本。

## 开发与验证

### 运行测试

在 Windows 桌面环境中运行本地测试：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v
```

测试使用本地数据和模拟服务，包含 Tkinter 桌面流程。v0.2.0 发布前的本地验证中，111 项测试通过；这不代表所有外部来源始终可连接。

### 构建 Windows 便携包

以下命令对应项目版本 `0.2.0`，先构建到暂存目录，再生成发布压缩包：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe release_package.py --prepare
.\.venv\Scripts\python.exe build_package.py --wait --dist-dir build/release-v0.2.0/dist
$packageCheck = Start-Process -FilePath ".\build\release-v0.2.0\dist\PaperHarbor\PaperHarbor.exe" -ArgumentList "--self-check", "--offline" -Wait -PassThru -WindowStyle Hidden
if ($packageCheck.ExitCode -ne 0) {
    throw "便携包自检失败，请检查 validation 中的报告"
} else {
    .\.venv\Scripts\python.exe release_package.py
}
```

命令等待自检进程退出，成功退出后再打包。自检报告位于 `build/release-v0.2.0/validation/packaged-check.json`，请另行查看其中记录的各项结果与截图错误；进程成功退出不代表每一项都通过。输出位于 `releases/`，包含 Windows ZIP 和 SHA256 校验文件。修改项目版本后，应同步调整命令中的暂存目录。

构建保留运行时的 HTTPS 证书校验配置；发布包仅包含运行程序与必要资源。

## 项目结构

```text
PaperHarbor/
├── app.py                    # 桌面界面
├── core.py                   # 记录、去重、配置、引用导出与报告
├── providers.py              # 检索、分页、缓存与来源状态
├── extra_providers.py        # 补充学术来源
├── publisher_catalog.py      # 出版社网站入口
├── downloader.py             # 正文与补充材料下载
├── launch.pyw                # Windows 启动入口
├── diagnostics.py            # 独立包自检
├── build_package.py          # EXE 构建
├── release_package.py        # 便携包打包
├── requirements.txt          # 运行依赖
├── requirements-build.txt    # 打包依赖
├── paperharbor.project.json  # 项目标识与版本
├── assets/                   # 应用图标
├── tests/                    # 测试
└── .github/workflows/ci.yml   # Windows 测试工作流
```

`data/`、`library/`、构建产物和本地验证记录不属于共享源码，已在 `.gitignore` 中排除。备份或迁移个人资料时，请另行保留 `data/` 与实际下载目录。

## 问题反馈

可通过 [GitHub Issues](https://github.com/lasheantpy/-/issues) 提交问题。请尽量提供软件版本、Windows 版本、复现步骤和界面错误提示；来源连接问题可附上「查看各来源状态」中的相关信息。

## v0.2.0 更新

- 扩展为六个自动检索来源，支持分页和最多 500 条结果。
- 添加九个出版社网站补查入口。
- 添加期刊、出版社与 ISSN 信息，以及当前结果筛选。
- 修正同名同年误合并与部分 arXiv 记录重复问题。
- 完善 DOI 精确查询、逐源状态、正文打开与下载报告。
