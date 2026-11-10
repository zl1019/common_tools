# 独立 Python 包发布指南

`common_tools` 是多包仓库。**每个发行包单独构建、检查和发布**，在目标包目录执行命令，不要从仓库根目录构建一个覆盖全部包的发行物。

## 发布单个包

例如发布 `wecom-sdk-python`。从 `common_tools` 仓库根目录执行：

```powershell
Set-Location wecom-sdk-python
python -m pip install -e ".[publish]"
python -m build
python -m twine check dist/*
```

生成的 wheel 和 source distribution 位于该包自己的 `dist/` 目录。发布其他包时进入它自己的项目目录，重复相同流程。

## 发布前检查

1. 确认目标包目录的 `pyproject.toml` 中发行名唯一、版本正确、Python 版本和依赖范围准确。
2. 确认 README、包发现规则、许可证和作者/维护者元数据与该包一致。许可证未确认时不要擅自声明。
3. 检查构建产物只包含该包需要的源码和文档；不要包含 `.env`、令牌、企业 CorpID/Secret、真实请求数据或其他包的构建产物。
4. PyPI 上已发布的版本不可覆盖；修订后提高包自己的版本号。
5. 安装发布产物的验证应在隔离环境内进行。按仓库约定，不在此流程中启动项目或编写单元测试。

## TestPyPI 与 PyPI

在目标包目录构建并检查后，可先上传 TestPyPI：

```powershell
python -m twine upload --repository testpypi dist/*
```

正式发布：

```powershell
python -m twine upload dist/*
```

上传时按提示输入用户名 `__token__` 和专用 token。不要将 token 写入命令、源码、依赖文件或 shell 历史。团队可配置 PyPI Trusted Publishing，避免保存长期上传令牌。

## 向消费者提供 GitHub 依赖

消费者的 `requirements.txt` 按包目录指定：

```text
发行名 @ git+https://github.com/zl1019/common_tools.git@版本标签或提交SHA#subdirectory=包项目目录
```

发布到 PyPI 后，消费者也可以直接按发行名和版本安装，不必依赖 GitHub 子目录。
