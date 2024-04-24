import os
import subprocess
import time

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

def download_video(url, save_path):
    response = requests.get(url)
    if response.status_code == 200:
        with open(save_path, 'wb') as f:
            f.write(response.content)
        return True
    else:
        return False

def process_video(inp, out):
    commands = [
        ['./spatial make -i {0}.MOV -f ou -o {1}.MOV --cdist 19.24 --hfov 63.4 --hadjust 0.02 --primary right --projection rect'.format(inp, out)],
    ]
    for command in commands:
        process = subprocess.Popen(command)
        process.wait()

        if process.returncode != 0:
            return False

    return True

@app.route('/process', methods=['POST'])
def processVideo():
    data = request.json
    video_url = data.get('url')
    if not video_url:
        return jsonify({'error': 'URL not provided'}), 400

    save_directory = '/downloads'
    video_name = 'video.mov'
    save_path = os.path.join(save_directory, video_name)

    success = download_video(video_url, save_path)
    if success:
        success = process_video(save_path, 'output')
        if not success:
            return jsonify({'error': 'Failed to process video'}), 500
        return jsonify({'output': 'output.MOV'}), 200
    else:
        return jsonify({'error': 'Failed to download video'}), 500

if __name__ == '__main__':
    app.run(debug=True, port=3000, host='0.0.0.0')
