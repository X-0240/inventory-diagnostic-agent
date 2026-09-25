"""错误类型与错误码：单独放一个模块，避免 facts 与 pipeline 互相导入。"""

class PlanError(RuntimeError):
    """带错误码的业务异常；错误码与契约「工具签名与错误语义」一节一致。"""
    def __init__(self,code,message):
        super().__init__(message)
        self.code=code
