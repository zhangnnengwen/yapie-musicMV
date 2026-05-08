#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Flask Web应用：视频加歌词字幕生成器
"""

import os
import subprocess
import uuid
from flask import Flask, render_template, request, redirect, url_for, send_from_directory, jsonify

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['OUTPUT_FOLDER'] = 'outputs'
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500MB

# 创建目录
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['OUTPUT_FOLDER'], exist_ok=True)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload():
    try:
        # 获取上传的文件
        video_file = request.files.get('video')
        lrc_file = request.files.get('lrc')
        
        if not video_file or video_file.filename == '':
            return jsonify({'success': False, 'message': '请上传视频文件'})
        
        if not lrc_file or lrc_file.filename == '':
            return jsonify({'success': False, 'message': '请上传歌词文件'})
        
        # 生成唯一ID
        task_id = str(uuid.uuid4())
        
        # 获取原始文件名并保存
        video_filename = video_file.filename
        lrc_filename = lrc_file.filename
        
        # 保存文件（保留原始扩展名）
        video_ext = os.path.splitext(video_filename)[1] or '.mp4'
        video_path = os.path.join(app.config['UPLOAD_FOLDER'], f'{task_id}_video{video_ext}')
        lrc_path = os.path.join(app.config['UPLOAD_FOLDER'], f'{task_id}_lyrics.lrc')
        output_path = os.path.join(app.config['OUTPUT_FOLDER'], f'{task_id}_output.mp4')
        
        video_file.save(video_path)
        lrc_file.save(lrc_path)
        
        # 获取参数
        font_size = request.form.get('fontsize', 48)
        font_color = request.form.get('fontcolor', 'white')
        position = request.form.get('position', 'bottom')
        margin = request.form.get('margin', 50)
        show_title = request.form.get('showtitle', 'on') == 'on'
        title_duration = request.form.get('titleduration', 5)
        
        # 构建命令
        cmd = [
            'D:/Python/python.exe', 'add_lyrics_to_video.py',
            '-v', video_path,
            '-l', lrc_path,
            '-o', output_path,
            '-fs', str(font_size),
            '-fc', font_color,
            '-pos', position,
            '-m', str(margin),
            '-td', str(title_duration)
        ]
        
        if not show_title:
            cmd.append('-notitle')
        
        # 执行命令
        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
        
        if result.returncode == 0:
            return jsonify({
                'success': True,
                'message': '视频生成成功',
                'download_url': url_for('download', filename=f'{task_id}_output.mp4')
            })
        else:
            return jsonify({
                'success': False,
                'message': f'视频生成失败: {result.stderr}'
            })
            
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/download/<filename>')
def download(filename):
    return send_from_directory(app.config['OUTPUT_FOLDER'], filename, as_attachment=True)

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)