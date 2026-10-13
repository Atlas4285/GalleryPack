# GalleryPack

使用方向键操作的漫画下载工具, 支持 E-Hentai 和禁漫天堂. 下载为文件夹, 自动生成 LANraragi GalleryDL 插件可读取的 `info.json`.

## 使用

安装 uv 后, 在项目目录执行:

```bash
uv sync
uv run gallerypack
```

选择下载来源, 每行输入一个目标, 支持多行粘贴:

- E-Hentai: 画廊链接, 如 `https://e-hentai.org/g/GID/token/`.
- 禁漫天堂: 编号或漫画链接, 如 `JM编号` 或 `https://18comic.vip/album/编号/`.

Enter 换行, Alt+Enter 开始下载. ↑↓ 滚动, Esc 返回或停止下载, Ctrl+C 退出. 再次提交相同目标会校验已有图片, 补下缺失或损坏页.

## 项目结构

| 路径                                                             | 内容                             |
| ---------------------------------------------------------------- | -------------------------------- |
| [`src/gallerypack/cli.py`](./src/gallerypack/cli.py)             | 命令行入口                       |
| [`src/gallerypack/ui/`](./src/gallerypack/ui/)                   | 菜单、输入和下载进度界面         |
| [`src/gallerypack/downloaders/`](./src/gallerypack/downloaders/) | E-Hentai、禁漫天堂下载及公共处理 |
| [`src/gallerypack/config.py`](./src/gallerypack/config.py)       | 配置加载、校验和保存             |
| [`src/gallerypack/naming.py`](./src/gallerypack/naming.py)       | 文件名校验和长度处理             |
| `config.json`                                                    | 首次运行生成的本地配置           |
| `download/`                                                      | 默认下载目录                     |

## License

[Apache-2.0](./LICENSE).
