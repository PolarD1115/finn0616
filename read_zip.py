import zipfile

zip_path = r'C:/Users/钟梓昕/Desktop/rikkahub/新网关/赛博宠物.zip'
files_to_read = ['manifest.json', 'supabase_schema.sql', 'main.js']

with zipfile.ZipFile(zip_path, 'r') as z:
    for fname in files_to_read:
        print(f'--- START OF {fname} ---')
        data = z.read(fname)
        print(data.decode('utf-8'))
        print(f'--- END OF {fname} ---')
        print()
