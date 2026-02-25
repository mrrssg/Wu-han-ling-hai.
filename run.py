from app import create_app

# 默认使用开发环境配置
app = create_app('development')

if __name__ == '__main__':
    # debug=True 表示修改代码后自动重启，且报错时网页会显示详细错误
    app.run(host='127.0.0.1', port=5000, debug=True)