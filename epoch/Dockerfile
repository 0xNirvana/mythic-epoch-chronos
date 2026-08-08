FROM itsafeaturemythic/mythic_python_base:latest

WORKDIR /Mythic/

COPY requirements.txt requirements.txt
RUN pip3 install -r requirements.txt

COPY . /Mythic/

CMD ["python3", "main.py"]
