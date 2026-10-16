"""Application failures shared by service and infrastructure adapters."""


class ServiceError(Exception):
    def __init__(self,code,message=None,status=400):
        self.code,self.message,self.status=code,message or code,status
        super().__init__(self.message)
