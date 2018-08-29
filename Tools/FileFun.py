# coding=utf-8
"""文件系统操作以及文件读写函数"""
import os
import io
import shutil
import json

import numpy as np
import pandas as pd

from QuantStudio.Tools.AuxiliaryFun import genAvailableName
from QuantStudio.Tools.DataTypeConversionFun import Series2DataFrame

# 复制文件夹到指定位置，如果该文件夹已经存在，则根据if_exist参数进行操作,replace表示覆盖原文件，skip表示保留原文件
def copyDir(source_dir,target_pos,if_exist='replace'):
    DirName = source_dir.split(os.sep)[-1]
    if DirName not in os.listdir(target_pos):
        os.mkdir(target_pos+os.sep+DirName)
    elif if_exist=='skip':
        return 0
    FileList = os.listdir(source_dir)
    for iFile in FileList:
        if os.path.isdir(source_dir+os.sep+iFile):
            copyDir(source_dir+os.sep+iFile, target_pos+os.sep+DirName,if_exist)
        elif (if_exist=='replace') or (iFile not in os.listdir(target_pos+os.sep+DirName)):
            shutil.copy(source_dir+os.sep+iFile, target_pos+os.sep+DirName+os.sep+iFile)
    return 0
# 清空目录下的内容
def clearDir(dir_path):
    AllFileNames = os.listdir(path=dir_path)
    nFailed = 0
    for iFileName in AllFileNames:
        iFilePath = dir_path+os.sep+iFileName
        if os.path.isdir(iFilePath):
            try:
                shutil.rmtree(iFilePath)
            except:
                nFailed += 1
        elif os.path.isfile(iFilePath):
            try:
                os.remove(iFilePath)
            except:
                nFailed += 1
    return nFailed
# 获取一个目录下所有的文件夹名称列表
def listDirDir(dir_path='.'):
    AllFileNames = os.listdir(path=dir_path)
    Rslt = []
    for iFileName in AllFileNames:
        if os.path.isdir(dir_path+os.sep+iFileName):
            Rslt.append(iFileName)
    return Rslt
# 获取一个目录下给定后缀的文件名称列表
def listDirFile(dir_path='.',suffix='csv'):
    suffix = suffix.lower()
    AllFileNames = os.listdir(path=dir_path)
    if suffix is None:
        return [iFileName for iFileName in AllFileNames if not os.path.isdir(dir_path+os.sep+iFileName)]
    else:
        Rslt = []
        if suffix=="":
            for iFileName in AllFileNames:
                iFileNameList = iFileName.split('.')
                if (len(iFileNameList)==1) and (not os.path.isdir(dir_path+os.sep+iFileName)):
                    Rslt.append(iFileName)
        else:
            for iFileName in AllFileNames:
                iFileNameList = iFileName.split('.')
                if (len(iFileNameList)>1) and (iFileNameList[-1].lower()==suffix):
                    Rslt.append('.'.join(iFileNameList[:-1]))
        return Rslt
# 遍历指定文件夹下的给定后缀的文件路径，可选是否遍历子文件夹,如果后缀名为None，遍历所有文件（不包括文件夹），后缀名为-1,遍历所有文件（包括文件夹）,后缀名为-2，遍历所有文件夹
def traverseDir(dir_path='.',suffix=None,traverse_subdir=True):
    if isinstance(suffix,str):
        suffix = suffix.lower()
    AllFileNames = os.listdir(path=dir_path)
    for iFileName in AllFileNames:
        iFilePath = dir_path+os.sep+iFileName
        if os.path.isdir(iFilePath):# 该元素是文件夹
            if isinstance(suffix,int) and (suffix<0):
                yield iFilePath
            if traverse_subdir:
                for jSubFilePath in traverseDir(dir_path=iFilePath,suffix=suffix,traverse_subdir=traverse_subdir):
                    yield jSubFilePath
            continue
        else:# 该元素是文件
            if suffix is None:
                yield iFilePath
            elif isinstance(suffix,int) and (suffix==-1):
                yield iFilePath
            elif isinstance(suffix,str):
                iFileNameList = iFileName.split('.')
                if ((suffix=='') and (len(iFileNameList)==1)) or ((len(iFileNameList)>1) and (iFileNameList[-1].lower()==suffix)):
                    yield iFilePath
                else:
                    continue
            else:
                continue
# 删除shelve文件
def deleteShelveFile(file_path):
    if os.path.isfile(file_path+".dat"):
        os.remove(file_path+".dat")
    if os.path.isfile(file_path+".bak"):
        os.remove(file_path+".bak")
    if os.path.isfile(file_path+".dir"):
        os.remove(file_path+".dir")
    return 0
# 输出字典序列到csv文件
def writeDictSeries2CSV(dict_series,file_path):
    Index = list(dict_series.index)
    Index.sort()
    nInd = len(Index)
    nLen = 0
    Lens = [len(dict_series[iInd]) for iInd in Index]
    nLen = max(Lens)
    DataArray = np.array([('',)*nInd*2]*(nLen+1),dtype='O')
    for i,iInd in enumerate(Index):
        DataArray[:Lens[i]+1,2*i:2*i+2] = np.array([(iInd,'')]+list(dict_series[iInd].items()))
    np.savetxt(file_path,DataArray,fmt='%s',delimiter=',')
    return 0
# 将函数定义写入文件,file_path:文件路径名;operator_info：算子定义信息，{'算子名称':'','算子定义':'','算子输入',['',''],'导入模块':[[],[]]}, to_truncate:是否清空文件
def writeFun2File(file_path,operator_info,to_truncate=True):
    Modules = {}# {父模块名:[(子模块名,模块别称)]}
    for i in range(len(operator_info['导入模块'][0])):
        if operator_info['导入模块'][0][i] in Modules:
            if (operator_info['导入模块'][1][i],operator_info['导入模块'][2][i]) not in Modules[operator_info['导入模块'][0][i]]:
                Modules[operator_info['导入模块'][0][i]].append((operator_info['导入模块'][1][i],operator_info['导入模块'][2][i]))
        else:
            Modules[operator_info['导入模块'][0][i]] = [(operator_info['导入模块'][1][i],operator_info['导入模块'][2][i])]
    # 书写导言区
    File = open(file_path,mode='a',encoding='utf-8')
    if to_truncate:
        File.truncate(0)
        File.writelines('# coding=utf-8')
    if '' in Modules:
        for iModule in Modules.pop(''):
            if iModule[1]=='':
                File.writelines('\nimport '+iModule[0])
            else:
                File.writelines('\nimport '+iModule[0]+' as '+iModule[1])
    for iSuperModule in Modules:
        for jSubModule,jModuleName in Modules[iSuperModule]:
            if jModuleName=='':
                File.writelines('\nfrom '+iSuperModule+' import '+jSubModule)
            else:
                File.writelines('\nfrom '+iSuperModule+' import '+jSubModule+' as '+jModuleName)
    # 书写函数头
    File.writelines('\ndef '+operator_info['算子名称']+'('+','.join(operator_info['算子输入'])+'):')
    # 书写函数体
    File.writelines(operator_info['算子定义'])
    File.flush()
    File.close()
    return 0
# 支持中文路径的 pandas 读取 csv 文件的函数
def readCSV2Pandas(filepath_or_buffer,detect_file_encoding=False,**other_args):
    if isinstance(filepath_or_buffer,str):# 输入的是文件路径
        with open(filepath_or_buffer,mode='rb') as File:
            filepath_or_buffer = File.read()
        if detect_file_encoding:
            import chardet
            Encoding = chardet.detect(filepath_or_buffer)
            other_args['encoding'] = Encoding["encoding"]
        filepath_or_buffer = io.BytesIO(filepath_or_buffer)
    Rslt = pd.read_csv(filepath_or_buffer,**other_args)
    return Rslt
# 获取系统偏好的文本编码格式
def guessSysTextEncoding():
    import locale
    import codecs
    return codecs.lookup(locale.getpreferredencoding()).name
# 查看文件的编码格式, 检测结果格式：{'confidence': 0.99, 'encoding': 'GB2312'}
def detectFileEncoding(file_path,big_file=False,size=None):
    if big_file:
        from chardet.universaldetector import UniversalDetector
        detector = UniversalDetector()#创建一个检测对象
        with open(file_path,mode='rb') as File:
            #分块进行测试，直到达到阈值
            if size is None:
                for line in File:
                    detector.feed(line)
                    if detector.done: break
            else:
                Batch = File.read(size=size)
                while Batch:
                    detector.feed(Batch)
                    if detector.done: break
                    Batch = File.read(size=size)
        detector.close()#关闭检测对象
        return detector.result
    else:
        import chardet
        with open(file_path,mode='rb') as File:
            if size is None:
                return chardet.detect(File.read())
            else:
                return chardet.detect(File.read(size))
# 将读入CSV文件（支持中文路径），形成DataFrame
def readCSV2StdDF(file_path,index='日期',col='字符串',encoding=None):
    if encoding is None:
        CSVFactor = readCSV2Pandas(file_path,detect_file_encoding=True,index_col=0,header=0)
    else:
        CSVFactor = readCSV2Pandas(file_path,detect_file_encoding=False,index_col=0,header=0,encoding=encoding)
    if index=='日期':
        DFIndex = [str(int(float(iDate))) for iDate in CSVFactor.index]
    elif index=='字符串':
        DFIndex = [str(iID) for iID in CSVFactor.index]
    elif index=='整数':
        DFIndex = [int(float(iID)) for iID in CSVFactor.index]
    elif index=="小数":
        DFIndex = [float(iID) for iID in CSVFactor.index]
    else:
        DFIndex = CSVFactor.index
    if index=='日期':
        DFCol = [str(int(float(iDate))) for iDate in CSVFactor.columns]
    elif index=='字符串':
        DFCol = [str(iID) for iID in CSVFactor.columns]
    elif index=='整数':
        DFCol = [int(float(iID)) for iID in CSVFactor.columns]
    elif index=="小数":
        DFCol = [float(iID) for iID in CSVFactor.columns]
    else:
        DFCol = CSVFactor.columns
    return pd.DataFrame(CSVFactor.values,index=DFIndex,columns=DFCol)
# 将CSV中的因子数据加载入内存
def loadCSVFactorData(csv_path):
    with open(csv_path,mode='rb') as File:
        if File.readline().split(b',')[0]==b'':
            Horizon = True
        else:
            Horizon = False
    if Horizon:
        try:
            CSVFactor = readCSV2Pandas(csv_path,index_col=0,header=0,encoding="utf-8")
        except:
            CSVFactor = readCSV2Pandas(csv_path,detect_file_encoding=True,index_col=0,header=0)
    else:
        try:
            CSVFactor = readCSV2Pandas(csv_path,header=0,encoding="utf-8")
        except:
            CSVFactor = readCSV2Pandas(csv_path,detect_file_encoding=True,header=0)
        Columns = list(CSVFactor.columns)
        CSVFactor = CSVFactor.set_index(Columns[:2])[Columns[2]]
        CSVFactor = Series2DataFrame(CSVFactor)
    CSVDate = [str(int(float(iDate))) for iDate in CSVFactor.index]
    CSVID = [str(iID) for iID in CSVFactor.columns]
    CSVFactor = pd.DataFrame(CSVFactor.values,index=CSVDate,columns=CSVID)
    return CSVFactor
# 将结果集写入 Excel 文件, output: {文件名: DataFrame}
def exportOutput2Excel(output, file_path=None):
    import xlwings as xw
    OutputNames = list(output.keys())
    OutputNames.sort()
    xlBook = xw.Book()
    nSheet = xlBook.sheets.count
    for i,iKey in enumerate(OutputNames):
        iOutput = output[iKey]
        if i<nSheet:
            xlBook.sheets[i].name = iKey
        else:
            xlBook.sheets.add(iKey)
        xlBook.sheets[iKey][0,0].options(pd.DataFrame,index=True,header=True).value = iOutput
    if file_path is None:
        return xlBook
    else:
        xlBook.save(path=file_path)
        xlBook.close()
        return 0
# 将结果集写入 CSV 文件, output: {文件名: DataFrame}
def exportOutput2CSV(output, dir_path="."):
    OutputNames = list(output.keys())
    OutputNames.sort()
    for i,iOutputName in enumerate(OutputNames):
        iOutput = output[iOutputName]
        iOutput.to_csv(dir_path+os.sep+iOutputName+".csv")
    return 0
# 读取json文件
def readJSONFile(file_path):
    if os.path.isfile(file_path):
        with open(file_path, "r", encoding="utf-8") as File:
            FileStr = File.read()
        if FileStr!="": return json.loads(FileStr)
    return {}