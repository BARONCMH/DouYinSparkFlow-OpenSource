抖音 Cookie 获取工具（Windows）

运行方式：双击 Get-Douyin-Cookies.exe，无需单独安装 Python。工具会打开独立的 Microsoft Edge 登录窗口。

1. 在弹出的抖音窗口登录自己的账号。
2. 检测到登录成功后，工具会在 EXE 所在文件夹生成 douyin_cookies.json。
3. 回到网站「授权登录 → 手动输入 Cookie」导入该文件内容。
4. 导入后删除 douyin_cookies.json。该文件等同于账号登录凭证。

隐私：浏览器使用全新隔离配置，不读取现有 Edge 配置或 Cookie。工具只读取 douyin.com 及其子域名的 Cookie，并只保存到本机；不会上传 Cookie。
