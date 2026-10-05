"""Build-time extension of the packaged noVNC viewer; never touches private data."""
from pathlib import Path
import shutil
import sys

MARKER='<!-- harun-office-mobile -->'

def install(root):
    root=Path(root)
    html=(root/'vnc.html').read_text()
    ui=(root/'app/ui.js').read_text()
    # Fail the build if the packaged UI is incompatible; don't ship a dead keyboard.
    for token in ('toggleVirtualKeyboard()', 'hideVirtualKeyboard()', 'export default UI'):
        if token not in ui:raise ValueError('Unsupported noVNC UI: '+token)
    if 'noVNC_keyboardinput' not in html or '</head>' not in html:
        raise ValueError('Unsupported noVNC HTML')
    for name in ('office-mobile.js','office-mobile.css'):
        shutil.copyfile(Path(__file__).parent/'novnc'/name,root/name)
    if MARKER not in html:
        html=html.replace('</head>',MARKER+'\n<link rel="stylesheet" href="office-mobile.css">\n'
                          '<script type="module" src="office-mobile.js"></script>\n</head>',1)
        (root/'vnc.html').write_text(html)

if __name__=='__main__':install(sys.argv[1])
