#!/bin/bash
if [ -f dist ]; then
    rm -r dist
fi

python3 setup.py sdist

cd dist
tar zxvf *.tar.gz

cd sipclients3-?.?.?

debuild --no-sign

ls

