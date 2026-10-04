"""Shared desktop geometry. Does not alter browser identity, profile or trading rules."""
import os
import re


def dimensions(environ=None):
    env=os.environ if environ is None else environ
    values=[]
    for key,default in [('OFFICE_DESKTOP_WIDTH','430'),('OFFICE_DESKTOP_HEIGHT','932')]:
        raw=env.get(key,default)
        if not re.fullmatch(r'[0-9]{3,4}',raw) or not 320<=int(raw)<=4096:
            raise ValueError(key+' harus bilangan bulat 320–4096')
        values.append(int(raw))
    return tuple(values)


def geometry(environ=None):
    width,height=dimensions(environ)
    return f'{width}x{height}x24'


def browser_options(environ=None):
    width,height=dimensions(environ)
    # Playwright's default fixed 1280px viewport would keep the site in desktop
    # layout even when Xvfb is portrait. Let the page follow the actual window.
    return {'no_viewport':True,'args':[f'--window-size={width},{height}',
                                     '--window-position=0,0','--force-device-scale-factor=1']}

if __name__=='__main__':print(geometry())
