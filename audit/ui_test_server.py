"""Isolated UI/HTTP test server. Synthetic translations, never touches live books."""
import argparse
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import yaml
import uvicorn
import book_translator as bt
import web_app as web
import web_features as api
import workspace as ws


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=18765);args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='book-ui-test-') as tmp:
        root=Path(tmp);cfg=bt.load_config(ROOT/'config.yaml');cfg.pop('_config_dir')
        (root/'config.yaml').write_text(yaml.safe_dump(cfg),encoding='utf-8')
        web.CONFIG_PATH=root/'config.yaml';web.SOURCES_DIR=root/'sources'
        text='\n\n'.join(f'CHAPTER {i}. The harbour\n\n'+('The sailor walked towards the old harbour and listened to the waves. '*18) for i in range(1,7))
        book=api.create_from_bytes(text.encode(),'test.txt','Проверка интерфейса','utf-8-sig',False,'[]')
        api.create_from_bytes(text.encode(),'manual.txt','Ручная разметка — проверка','utf-8-sig',False,'[]','manual','[]',False)
        conn,_=web.open_project_connection(book['name'])
        ws.save_version(conn,1,'ГЛАВА 1. Гавань\n\nМоряк шёл к старой гавани и слушал шум волн.\n\nЭто сохранённый текст для проверки редактора.','machine');conn.commit();conn.close()
        def translate(self,model,source):
            time.sleep(.25)
            return ('Моряк шёл к старой гавани и слушал шум волн. '*max(1,len(source)//50),100,120)
        with patch.object(bt.LMStudioClient,'list_models',return_value=[cfg['lmstudio']['model']]),patch.object(bt.LMStudioClient,'list_loaded_models',return_value=[cfg['lmstudio']['model']]),patch.object(bt.LMStudioClient,'translate',translate):
            uvicorn.run(web.app,host='127.0.0.1',port=args.port,log_level='error')


if __name__=='__main__':main()
