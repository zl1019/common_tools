# OpenDay Common Tools

本目录采用 monorepo 管理多个可独立安装、独立版本化和独立发布的 Python 发行包。每个包放在自己的项目目录中，并在该目录维护 `pyproject.toml`、README 和源码。

## 包清单

| 项目目录 | PyPI 发行名 | Python 导入名 | 说明 |
|---|---|---|---|
| [`wecom-sdk-python/`](wecom-sdk-python/) | `wecom-sdk-python` | `wecom_sdk` | 企业微信 API 异步 SDK |

## 新增包约定

新增独立发行包时，创建自己的项目目录，例如：

```text
common_tools/
  another-sdk/
    pyproject.toml
    README.md
    src/
      another_sdk/
        __init__.py
  wecom-sdk-python/
    pyproject.toml
    README.md
    wecom_sdk/
      __init__.py
```

每个项目目录独立维护发行名、版本、依赖、构建配置和发布流程。推荐新包采用 `src/` 布局；现有 `wecom-sdk-python` 保留其已形成的源码布局。仓库根目录不再定义一个覆盖所有包的 Python 发行项目。

## 从 GitHub 安装包

在应用的 `requirements.txt` 中按包分别指定仓库子目录：

```text
wecom-sdk-python @ git+https://github.com/szvayi/OpenDay.git@main#subdirectory=common_tools/wecom-sdk-python
```

建议把 `main` 固定为 release tag 或 commit SHA，以避免依赖随分支更新漂移。私有仓库需在运行环境中配置 GitHub 认证，不要把访问令牌写入依赖文件。

具体 SDK 使用方法见包自己的 [README](wecom-sdk-python/README.md)。
