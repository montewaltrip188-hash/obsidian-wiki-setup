import zipfile, os, shutil
dest = os.path.expanduser("~/Documents/ObsidianVault")
if os.path.exists(dest):
    shutil.rmtree(dest)
z = zipfile.ZipFile(os.path.expanduser("~/Downloads/OB/install/vault.zip"))
for info in z.infolist():
    try:
        fn = info.filename.encode("cp437").decode("gbk")
    except:
        fn = info.filename
    fn = fn.replace("\\", "/")
    if fn.endswith("/"):
        os.makedirs(os.path.join(dest, fn), exist_ok=True)
    else:
        path = os.path.join(dest, fn)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.isdir(path):
            with open(path, "wb") as f:
                f.write(z.read(info))
print("知识库部署完成:", dest)
