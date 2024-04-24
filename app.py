import os
import subprocess
import time
import boto3

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

def download_video(url, save_path):
    response = requests.get(url)
    print(response.status_code)
    if response.status_code == 200:
        with open(save_path, 'wb') as f:
            f.write(response.content)
        return True
    else:
        return False

def process_video(inp, out):
    commands = [
        ['./spatial make -i {0} -f ou -o {1} --cdist 19.24 --hfov 63.4 --hadjust 0.02 --primary right --projection rect'.format(inp, out)],
    ]
    for command in commands:
        process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        print('Running command: {}'.format(command))
        print('Output: {}'.format(process.stdout.read()))
        process.wait()

        if process.returncode != 0:
            print('Error: {}'.format(process.stderr.read()))
            return False
        
        s3 = boto3.client('s3')
        try:
            with open(out, 'rb') as data:
                s3.upload_fileobj(data, "smoovie-gen-video", out)
        except Exception as e:
            print(f"Error uploading file to S3: {e}")
            return False


    return True

@app.route('/process', methods=['POST'])
def processVideo():
    data = request.json
    video_url = data.get('url')
    if not video_url:
        return jsonify({'error': 'URL not provided'}), 400

    save_directory = '\\downloads'
    # get extension from url
    video_name = 'test' + str(int(time.time())) + '.' + video_url.split('.')[-1] 

    success = download_video(video_url, video_name)
    if success:
        success = process_video(video_name, 'output.mov')
        if not success:
            return jsonify({'error': 'Failed to process video'}), 500
        return jsonify({'output': 'output.mov'}), 200
    else:
        return jsonify({'error': 'Failed to download video'}), 500
    
@app.route('/', methods=['GET'])
def test():
    return jsonify({'message': 'Hello World!'})

if __name__ == '__main__':
    app.run(debug=True, port=3000, host='0.0.0.0')
