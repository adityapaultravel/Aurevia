import sys, traceback
sys.path.insert(0, r'C:\Users\ADI\Documents\project\backend')
try:
    import server
    print('Imported server OK')
except Exception:
    with open('import_error.txt', 'w', encoding='utf-8') as f:
        traceback.print_exc(file=f)
    raise
