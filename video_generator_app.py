#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RunningHub AI应用高级API调用服务
支持自定义ComfyUI工作流参数
"""

import os
import requests
import json
from flask import Flask, render_template, request, jsonify, send_from_directory
import base64

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['OUTPUT_FOLDER'] = 'outputs'

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['OUTPUT_FOLDER'], exist_ok=True)

API_BASE_URL = 'https://www.runninghub.cn'

@app.route('/')
def index():
    return render_template('video_generator.html')

@app.route('/api/get-app-info', methods=['GET'])
def get_app_info():
    """获取AI应用信息"""
    try:
        api_key = request.args.get('apiKey')
        webapp_id = request.args.get('webappId')
        
        if not api_key or not webapp_id:
            return jsonify({'success': False, 'message': '请提供API Key和WebApp ID'})
        
        url = f'{API_BASE_URL}/api/webapp/apiCallDemo'
        params = {
            'apiKey': api_key,
            'webappId': webapp_id
        }
        
        response = requests.get(url, params=params)
        
        if response.status_code == 200:
            result = response.json()
            if result.get('code') == 0:
                return jsonify({
                    'success': True,
                    'webappName': result['data'].get('webappName'),
                    'nodeInfoList': result['data'].get('nodeInfoList', []),
                    'covers': result['data'].get('covers', [])
                })
            else:
                return jsonify({
                    'success': False,
                    'message': result.get('msg', '获取应用信息失败')
                })
        else:
            return jsonify({
                'success': False,
                'message': f'API调用失败: {response.status_code}'
            })
            
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/api/generate-video', methods=['POST'])
def generate_video():
    """发起AI应用任务"""
    try:
        api_key = request.form.get('apiKey')
        webapp_id = request.form.get('webappId')
        node_info_list_json = request.form.get('nodeInfoList', '[]')
        
        if not api_key or not webapp_id:
            return jsonify({'success': False, 'message': '请提供API Key和WebApp ID'})
        
        # 解析nodeInfoList
        try:
            node_info_list = json.loads(node_info_list_json)
        except:
            return jsonify({'success': False, 'message': '节点参数格式错误'})
        
        # 处理文件上传
        files = {}
        for key in request.files:
            file = request.files[key]
            if file:
                # 保存文件
                filename = f'{os.urandom(8).hex()}_{file.filename}'
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                file.save(filepath)
                files[key] = (filename, open(filepath, 'rb'), file.content_type)
        
        # 构建请求
        url = f'{API_BASE_URL}/task/openapi/ai-app/run'
        
        data = {
            'apiKey': api_key,
            'webappId': int(webapp_id),
            'nodeInfoList': json.dumps(node_info_list)
        }
        
        # 发送请求
        if files:
            response = requests.post(url, data=data, files=files)
            # 关闭文件句柄
            for f in files.values():
                f[1].close()
        else:
            headers = {'Content-Type': 'application/json'}
            response = requests.post(url, json=data, headers=headers)
        
        if response.status_code == 200:
            result = response.json()
            if result.get('code') == 0:
                return jsonify({
                    'success': True,
                    'message': '任务创建成功',
                    'taskId': result['data'].get('taskId'),
                    'taskStatus': result['data'].get('taskStatus'),
                    'netWssUrl': result['data'].get('netWssUrl')
                })
            else:
                return jsonify({
                    'success': False,
                    'message': result.get('msg', '任务创建失败')
                })
        else:
            return jsonify({
                'success': False,
                'message': f'API调用失败: {response.status_code}'
            })
            
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/api/task-status/<task_id>', methods=['GET'])
def get_task_status(task_id):
    """查询任务状态"""
    try:
        api_key = request.args.get('apiKey')
        
        if not api_key:
            return jsonify({'success': False, 'message': '请提供API Key'})
        
        url = f'{API_BASE_URL}/openapi/v2/task/status'
        params = {
            'taskId': task_id,
            'apiKey': api_key
        }
        
        response = requests.get(url, params=params)
        
        if response.status_code == 200:
            result = response.json()
            if result.get('code') == 0:
                task_data = result['data']
                return jsonify({
                    'success': True,
                    'taskStatus': task_data.get('taskStatus'),
                    'resultUrl': task_data.get('resultUrl'),
                    'progress': task_data.get('progress', 0),
                    'outputs': task_data.get('outputs', [])
                })
            else:
                return jsonify({
                    'success': False,
                    'message': result.get('msg', '查询失败')
                })
        else:
            return jsonify({
                'success': False,
                'message': f'API调用失败: {response.status_code}'
            })
            
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5001)
