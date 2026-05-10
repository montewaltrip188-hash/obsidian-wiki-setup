import zipfile, os
z = zipfile.ZipFile(os.path.expanduser("~/Downloads/OB/install/vault.zip"))
dest = os.path.expanduser("~/Documents/ObsidianVault")
for info in z.infolist():
    try:
        fn = info.filename.encode("cp437").decode("gbk")
    except:
        fn = info.filename
    fn = fn.replace("\\", "/")
    path = os.path.join(dest, fn)
    if info.is_dir():
        os.makedirs(path, exist_ok=True)
    else:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(z.read(info))
print("知识库部署完成:", dest)
