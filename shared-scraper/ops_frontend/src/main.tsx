import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { AuthProvider } from '../../tm_manager/frontend/src/context/AuthContext';
import { ToastProvider } from '../../tm_manager/frontend/src/components/tm/Toast';
import { ConfirmProvider } from '../../tm_manager/frontend/src/components/tm/ConfirmDialog';
import App from './App';
import '../../tm_manager/frontend/src/index.css';
import './style.css';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode><BrowserRouter><AuthProvider><ToastProvider><ConfirmProvider><App /></ConfirmProvider></ToastProvider></AuthProvider></BrowserRouter></React.StrictMode>
);
